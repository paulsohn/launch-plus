"""Shared helpers for invoking rosdep from both the resolver and builder.

Ported from crates/roscope-core/src/rosdep.rs.

Uses ``rosdep resolve`` to map rosdep keys to system package names, then
installs via ``apt-get``/``pip`` directly.

**Why not ``rosdep install``?**  Without ``--from-paths``, ``rosdep install``
treats arguments as ROS package names and fails on pure system keys.
With ``--from-paths``, it scans every ``package.xml`` and pulls in
unresolvable test-only dependencies.  Neither mode works for our use case.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from roscope.exceptions import ProcessExecutionError

logger = logging.getLogger(__name__)

# Module-level singletons (like Rust's Once/OnceLock)
_rosdep_updated = False
_rosdep_update_lock = threading.Lock()
_installed_packages_cache: set[str] | None = None
_installed_packages_lock = threading.Lock()
_break_system_packages: bool | None = None


def _pip_supports_break_system_packages() -> bool:
    """Check if pip supports --break-system-packages (pip >= 23.0.1, Ubuntu 24.04+)."""
    global _break_system_packages  # noqa: PLW0603
    if _break_system_packages is not None:
        return _break_system_packages
    try:
        result = subprocess.run(
            ["pip", "install", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        _break_system_packages = "--break-system-packages" in result.stdout
    except OSError:
        _break_system_packages = False
    return _break_system_packages


def ensure_rosdep_updated() -> None:
    """Run ``rosdep update`` once per process (best-effort, non-fatal)."""
    global _rosdep_updated  # noqa: PLW0603
    if _rosdep_updated:
        return
    with _rosdep_update_lock:
        if _rosdep_updated:
            return
        logger.info("Running one-time rosdep update")
        try:
            subprocess.run(["rosdep", "update"], check=False, capture_output=True)
        except OSError:
            pass
        _rosdep_updated = True


def _ros_distro() -> str | None:
    """Return the value of ``$ROS_DISTRO``, or ``None`` if unset/empty."""
    val = os.environ.get("ROS_DISTRO", "")
    return val if val else None


# ---------------------------------------------------------------------------
# ResolvedDeps
# ---------------------------------------------------------------------------


@dataclass
class ResolvedDeps:
    """System packages grouped by installer."""

    apt: list[str] = field(default_factory=list)
    """Packages to install via ``apt-get install``."""

    pip: list[str] = field(default_factory=list)
    """Packages to install via ``pip install``."""

    unresolved: list[str] = field(default_factory=list)
    """Rosdep keys that could not be resolved."""


# ---------------------------------------------------------------------------
# rosdep resolve
# ---------------------------------------------------------------------------

_SUPPORTED_INSTALLERS = frozenset({"apt", "pip"})


def _is_supported_installer(installer: str) -> bool:
    return installer in _SUPPORTED_INSTALLERS


def _collect_packages(result: ResolvedDeps, installer: str, packages_line: str) -> None:
    """Add space-separated package names to the appropriate installer bucket."""
    if installer == "apt":
        target = result.apt
    elif installer == "pip":
        target = result.pip
    else:
        return
    for pkg in packages_line.split():
        if pkg:
            target.append(pkg)


def _parse_rosdep_resolve(stdout: str, keys: list[str]) -> ResolvedDeps:
    """Parse the stdout of ``rosdep resolve``."""
    result = ResolvedDeps()
    lines = stdout.splitlines()

    if not lines:
        result.unresolved = list(keys)
        return result

    # Single-key format: no #ROSDEP[...] headers
    if len(keys) == 1 and not lines[0].startswith("#ROSDEP["):
        installer: str | None = None
        has_supported = False
        for line in lines:
            if line.startswith("#"):
                installer = line[1:]
                if _is_supported_installer(installer):
                    has_supported = True
            elif installer is not None:
                _collect_packages(result, installer, line)
        if not has_supported:
            result.unresolved.append(keys[0])
        return result

    # Multi-key format: #ROSDEP[key] headers
    seen_keys: set[str] = set()
    installer = None
    current_key_has_installer = False
    current_key: str | None = None

    for line in lines:
        if line.startswith("#ROSDEP["):
            # Flush previous key
            if current_key is not None and not current_key_has_installer:
                result.unresolved.append(current_key)
            key = line[len("#ROSDEP[") :].rstrip("]")
            seen_keys.add(key)
            current_key = key
            current_key_has_installer = False
            installer = None
        elif line.startswith("#"):
            installer = line[1:]
            if _is_supported_installer(installer):
                current_key_has_installer = True
        elif installer is not None:
            _collect_packages(result, installer, line)

    # Flush last key
    if current_key is not None and not current_key_has_installer:
        result.unresolved.append(current_key)

    # Keys not even mentioned in the output
    for key in keys:
        if key not in seen_keys:
            result.unresolved.append(key)

    return result


def rosdep_resolve(keys: list[str]) -> ResolvedDeps:
    """Resolve rosdep keys to system package names via ``rosdep resolve``.

    Calls :func:`ensure_rosdep_updated` first.
    """
    if not keys:
        return ResolvedDeps()

    distro = _ros_distro()
    if distro is None:
        raise ProcessExecutionError("ROS_DISTRO is not set; cannot resolve rosdep keys")

    ensure_rosdep_updated()

    args = ["resolve", "--rosdistro", distro, *keys]
    try:
        result = subprocess.run(
            ["rosdep", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise ProcessExecutionError(f"failed to run rosdep resolve: {e}") from e

    resolved = _parse_rosdep_resolve(result.stdout, keys)

    # Parse stderr for unresolved keys
    if result.returncode != 0:
        for line in result.stderr.splitlines():
            prefix = "ERROR: no rosdep rule for '"
            if line.startswith(prefix):
                key = line[len(prefix) :].rstrip("'")
                if key not in resolved.unresolved:
                    resolved.unresolved.append(key)

    return resolved


# ---------------------------------------------------------------------------
# installed packages detection
# ---------------------------------------------------------------------------


def _discover_installed_packages() -> set[str]:
    """Collect package names available in AMENT_PREFIX_PATH and ROS_PACKAGE_PATH."""
    pkgs: set[str] = set()

    # AMENT_PREFIX_PATH: <prefix>/share/<pkg_name>/package.xml
    ament_path = os.environ.get("AMENT_PREFIX_PATH", "")
    for prefix in ament_path.split(":"):
        if not prefix:
            continue
        share = Path(prefix) / "share"
        if not share.is_dir():
            continue
        try:
            for entry in share.iterdir():
                if entry.is_dir() and (entry / "package.xml").exists():
                    pkgs.add(entry.name)
        except OSError:
            pass

    # ROS_PACKAGE_PATH: catkin-style
    ros_path = os.environ.get("ROS_PACKAGE_PATH", "")
    for dir_str in ros_path.split(":"):
        if not dir_str:
            continue
        d = Path(dir_str)
        if (d / "package.xml").exists():
            pkgs.add(d.name)
        try:
            for entry in d.iterdir():
                if entry.is_dir() and (entry / "package.xml").exists():
                    pkgs.add(entry.name)
        except OSError:
            pass

    return pkgs


def installed_packages() -> set[str]:
    """Return cached set of installed ROS packages."""
    global _installed_packages_cache  # noqa: PLW0603
    if _installed_packages_cache is not None:
        return _installed_packages_cache
    with _installed_packages_lock:
        if _installed_packages_cache is not None:
            return _installed_packages_cache
        _installed_packages_cache = _discover_installed_packages()
        return _installed_packages_cache


# ---------------------------------------------------------------------------
# rosdep install
# ---------------------------------------------------------------------------


def rosdep_install(keys: list[str]) -> None:
    """Resolve rosdep keys and install the resulting system packages.

    1. Filter out keys already available as installed ROS packages
    2. ``rosdep resolve`` maps remaining keys to system package names
    3. ``apt-get install`` for ``#apt`` packages
    4. ``pip install`` for ``#pip`` packages
    """
    if not keys:
        return

    # Filter out already-installed keys
    installed = installed_packages()
    filtered = sorted(k for k in keys if k not in installed)

    if len(filtered) < len(keys):
        logger.info(
            "Skipped %d keys already installed as ROS packages (%d remaining)",
            len(keys) - len(filtered),
            len(filtered),
        )

    if not filtered:
        return

    logger.info("Resolving %d rosdep keys", len(filtered))
    resolved = rosdep_resolve(filtered)

    if resolved.unresolved:
        raise ProcessExecutionError(
            f"rosdep could not resolve {len(resolved.unresolved)} keys: {resolved.unresolved}"
        )

    # Deduplicate and sort
    apt_pkgs = sorted(set(resolved.apt))
    pip_pkgs = sorted(set(resolved.pip))

    if apt_pkgs:
        logger.info("Installing %d apt packages", len(apt_pkgs))
        proc = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "--no-install-recommends", *apt_pkgs],
            check=False,
        )
        if proc.returncode != 0:
            raise ProcessExecutionError(f"apt-get install failed (exit {proc.returncode})")

    if pip_pkgs:
        logger.info("Installing %d pip packages", len(pip_pkgs))
        cmd = ["pip", "install"]
        if _pip_supports_break_system_packages():
            cmd.append("--break-system-packages")
        cmd.extend(pip_pkgs)
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise ProcessExecutionError(f"pip install failed (exit {proc.returncode})")
