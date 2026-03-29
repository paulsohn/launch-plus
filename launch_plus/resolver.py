#!/usr/bin/env python3
"""
launch-plus Python launch file resolver.

Executes a .launch.py file with import-patching hooks that intercept
launch_ros and launch classes to track package dependencies.

Usage:
    python3 py_resolver.py <launch_file> <args_json>

Output (JSON to stdout):
    {
        "packages": ["pkg_a", "pkg_b"],
        "includes": ["path/to/other.launch.py"],
        "nodes": [{"package": "...", "executable": "...", "name": "..."}],
        "warnings": ["..."]
    }
"""

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

# Ensure substitution entity classes are registered before any parsing occurs.
import launch_plus.entities  # noqa: F401
from launch_plus.entities.substitution import Substitution as _SubstitutionType

# ─── Output schema contract ───────────────────────────────────────────────────
# Defines the exact JSON schema emitted to stdout.  Rust's PyResolvedNode /
# PyResolverOutput structs must mirror these types.  Guarded by TYPE_CHECKING
# so there is zero runtime cost; mypy / pyright will enforce the contract.
if TYPE_CHECKING:
    from typing import TypedDict

    class _PluginDict(TypedDict):
        package: str
        plugin: str
        name: str | None
        parameters: dict[str, str]
        remappings: list[list[str]]  # [[src, dst], ...]

    class _NodeDict(TypedDict):
        package: str
        executable: str
        name: str
        namespace_stack: list[str]  # PushRosNamespace stack at resolution time
        explicit_namespace: str | None  # node's own namespace= kwarg, resolved
        parameters: dict[str, str]
        param_files: list[dict]  # [{"path": str, "params": ...?}, ...]
        remappings: list[list[str]]  # [[src, dst], ...]
        env: dict[str, str]
        kind: str  # "node" | "container" | "load_composable"
        plugins: list[_PluginDict]
        target: str | None

    class _DeclaredArgDict(TypedDict):
        name: str
        default: str

    class _ResolverOutput(TypedDict):
        packages: list[str]
        includes: list[str]
        nodes: list[_NodeDict]
        warnings: list[str]
        errors: list[str]
        declared_args: list[_DeclaredArgDict]
        global_params: list[list]
        include_args: dict[str, dict[str, str]]
        param_files: list[str]
        set_launch_configurations: dict[str, str]

# ─── Capture real ament_index_python BEFORE installing the import patcher ────
# This allows FindPackageShare.perform() to return real installed paths when
# the package is actually present in the build/install tree.

_real_get_package_share_directory = None
try:
    from ament_index_python.packages import get_package_share_directory as _real_gps

    _real_get_package_share_directory = _real_gps
except Exception:
    pass

# ─── ROS prefix fallback ─────────────────────────────────────────────────────
# Use $ROS_DISTRO env var so this works across distro versions (jazzy, rolling, etc.)
_ROS_DISTRO = os.environ.get("ROS_DISTRO", "")
_ROS_DISTRO_PREFIX = f"/opt/ros/{_ROS_DISTRO}" if _ROS_DISTRO else ""


def _parse_rosdep_resolve(stdout: str) -> list[str]:
    """Parse ``rosdep resolve`` stdout into a list of apt package names.

    Output format::

        #apt
        ros-humble-ublox-gps

    or with multiple packages on one line::

        #apt
        libnl-3-dev libnl-genl-3-dev

    Only ``#apt`` packages are returned; other installers (``#pip``, ``#brew``)
    are ignored.
    """
    apt_pkgs: list[str] = []
    installer = ""
    for line in stdout.strip().splitlines():
        if line.startswith("#"):
            installer = line.lstrip("#").strip()
        elif installer == "apt" and line.strip():
            apt_pkgs.extend(line.strip().split())
    return apt_pkgs


def _try_rosdep_install(package: str) -> bool:
    """Resolve a rosdep key to system packages and install them.

    Uses ``rosdep resolve`` + ``apt-get install`` (matching the Rust-side
    approach in rosdep.rs) instead of ``rosdep install`` which treats
    arguments as ROS package names and fails on plain keys.
    """
    if package in _state.rosdep_attempted:
        return False
    _state.rosdep_attempted.add(package)
    ros_distro = os.environ.get("ROS_DISTRO", "")
    if not ros_distro:
        return False
    try:
        # Step 1: rosdep resolve → find apt package name.
        resolve = subprocess.run(
            ["rosdep", "resolve", "--rosdistro", ros_distro, package],
            capture_output=True,
            text=True,
        )
        if resolve.returncode != 0:
            return False
        apt_pkgs = _parse_rosdep_resolve(resolve.stdout)
        if not apt_pkgs:
            return False
        # Step 2: apt-get install.
        install = subprocess.run(
            ["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", *apt_pkgs],
            capture_output=True,
            text=True,
        )
        return install.returncode == 0
    except Exception:
        return False


class _PackageNotFetchedError(RuntimeError):
    """Raised when a lockfile package cannot be fetched and resolution must abort.

    This is now a last-resort exception: _ensure_fetched() handles inline fetching
    for most cases.  This exception is only raised when fetching actually fails
    (e.g. git error, package not in lockfile but expected).
    """

    def __init__(self, pkg_name: str):
        super().__init__(f"package '{pkg_name}' source not on disk; fetch failed")
        self.pkg_name = pkg_name


def _ensure_fetched(package: str) -> bool:
    """Ensure a lockfile package is fully fetched (has package.xml on disk).

    Performs inline git sparse-checkout to fetch the package directory if needed.
    Updates _state.package_shares with the on-disk path after fetching.

    Returns True if the package is available after this call.
    Returns False if the package is not in the lockfile or fetching failed.
    """
    if package in _state.fetched_packages:
        return True

    pkg_info = _state.lockfile_data.get(package)
    if not pkg_info or not _state.fetch_dir:
        return False

    repo_workspace_path = pkg_info["repo"]
    pkg_path_in_repo = pkg_info["path"]
    repo_url = pkg_info["url"]
    repo_sha = pkg_info["version"]

    repo_dir = os.path.join(_state.fetch_dir, repo_workspace_path)
    pkg_dir = os.path.join(repo_dir, pkg_path_in_repo)

    # Check if already fully fetched (package.xml present).
    if os.path.isfile(os.path.join(pkg_dir, "package.xml")):
        _state.fetched_packages.add(package)
        _state.package_shares[package] = pkg_dir
        return True

    # Need to fetch via git sparse-checkout.
    try:
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            # Repo not cloned yet — sparse clone.
            os.makedirs(repo_dir, exist_ok=True)
            # Normalize sparse path: "." means repo root → use "/**"
            sparse_path = "/**" if pkg_path_in_repo in (".", "") else pkg_path_in_repo
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--sparse",
                    "--single-branch",
                    "--depth=1",
                    repo_url,
                    repo_dir,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "sparse-checkout", "set", "--no-cone", sparse_path],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "checkout", repo_sha],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            # Repo exists — add to sparse-checkout.
            sparse_path = "/**" if pkg_path_in_repo in (".", "") else pkg_path_in_repo
            subprocess.run(
                ["git", "sparse-checkout", "add", sparse_path],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )

        # Verify fetch succeeded.
        if os.path.isfile(os.path.join(pkg_dir, "package.xml")):
            _state.fetched_packages.add(package)
            _state.package_shares[package] = pkg_dir
            return True
        else:
            _warn(f"fetched package '{package}' but package.xml not found at {pkg_dir}")
            return False

    except subprocess.CalledProcessError as e:
        _warn(f"git fetch failed for package '{package}': {e.stderr or e}")
        return False
    except Exception as e:
        _warn(f"failed to fetch package '{package}': {e}")
        return False


# ─── Portable path support ────────────────────────────────────────────────────
#
# A "portable path" is a string in $(find-pkg-share <pkg>)/... format.  It is
# the canonical path representation used internally by launch-plus in all modes.
# Actual filesystem resolution is deferred to the two sites that require it:
#   1. Include expansion (Rust orchestrator resolves to read the included file).
#   2. File access inside OpaqueFunction bodies (via _stub_open / os.path stubs).
#
# This regex extracts the package name and the optional suffix from a portable path.

_PORTABLE_PATH_RE = re.compile(r"^\$\(find-pkg-share ([^)]+)\)(.*)")


def _parse_portable_path(path_str: str):
    """Extract ``(pkg_name, rest)`` from a portable path string, or return ``None``.

    ``rest`` is the portion after the closing ``)`` with any leading separator stripped.
    For example::

        "$(find-pkg-share my_pkg)/config/file.yaml"  →  ("my_pkg", "config/file.yaml")
        "$(find-pkg-share my_pkg)"                   →  ("my_pkg", "")
        "/absolute/path"                             →  None
    """
    m = _PORTABLE_PATH_RE.match(path_str)
    if not m:
        return None
    pkg = m.group(1).strip()
    rest = m.group(2).lstrip("/").lstrip(os.sep)
    return pkg, rest


def _resolve_pkg_share(package: str) -> str:
    """Resolve a package share path.

    Preview mode (lockfile workflow):
      Lockfile packages resolve from the workspace source tree.  If the source
      directory is not yet fully fetched (no package.xml), _ensure_fetched() is
      called to fetch it inline.

    Non-preview mode (postbuild):
      Only AMENT_PREFIX_PATH (installed artifacts) is used.  Source paths are
      never returned — if a package is not installed, the portable fallback is
      returned.

    Non-lockfile packages (system / rosdep) are resolved via AMENT_PREFIX_PATH
    in both modes.
    """
    # 1. _state.package_shares lookup.
    #    Preview: contains workspace source paths.
    #    Postbuild: contains install paths from AMENT (populated by orchestrator).
    if package in _state.package_shares:
        pkg_path: str = _state.package_shares[package]
        # In preview mode, lockfile packages may need full fetch.
        if (
            _state.preview_mode
            and _state.lockfile_data
            and package in _state.lockfile_data
            and not os.path.isfile(os.path.join(pkg_path, "package.xml"))
        ):
            if _ensure_fetched(package):
                return str(_state.package_shares[package])
            raise _PackageNotFetchedError(package)
        return pkg_path
    # 2. Lockfile package not yet in _state.package_shares — fetch (preview only).
    if _state.preview_mode and _state.lockfile_data and package in _state.lockfile_data:
        if _ensure_fetched(package):
            return str(_state.package_shares[package])
        raise _PackageNotFetchedError(package)
    # 3. Non-lockfile packages (system / rosdep): use AMENT_PREFIX_PATH.
    if _real_get_package_share_directory is not None:
        try:
            return str(_real_get_package_share_directory(package))
        except _PackageNotFetchedError:
            raise
        except Exception:
            pass  # Fall through to rosdep or portable fallback.
    # 4. Try rosdep install if enabled.
    if (
        _state.rosdep_fallback
        and _try_rosdep_install(package)
        and _real_get_package_share_directory is not None
    ):
        try:
            return str(_real_get_package_share_directory(package))
        except Exception:
            pass
    # 5. Fallback.
    if _state.preview_mode:
        # Preview: return portable syntax — downstream open() will hit the
        # FileNotFoundError stub which emits a warning.
        return f"$(find-pkg-share {package})"
    # Postbuild: package not installed — this is an error.
    raise LookupError(f"package '{package}' not found in AMENT_PREFIX_PATH")


def _resolve_ros_substitutions(value: str) -> str:
    """Replace $(find-pkg-share X) patterns in a string with real paths."""
    return re.sub(
        r"\$\(find-pkg-share ([^)]+)\)",
        lambda m: _resolve_pkg_share(m.group(1).strip()),
        value,
    )


# ─── Result accumulator ──────────────────────────────────────────────────────


class ResolverState:
    """Bundles all mutable module-level resolver state.

    A single ``_state`` instance is created at module level.  All resolver
    functions access state through ``_state.X`` instead of bare globals.
    The test fixture resets state by calling ``_state.reset()``.
    """

    __slots__ = (
        "tracked",
        "declared_arg_names",
        "namespace_stack",
        "include_chain",
        "env",
        "inline_params",
        "global_params",
        "global_remaps",
        "global_param_files",
        "package_shares",
        "apply_opaque_file_access",
        "preview_mode",
        "lockfile_data",
        "rosdep_fallback",
        "apply_arg_defaults",
        "global_arg_cascade",
        "allow_unportable_paths",
        "fetch_dir",
        "fetched_packages",
        "rosdep_attempted",
        "root_source_key",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Reset all state to initial values."""
        self.tracked: dict[str, Any] = {
            "packages": [],
            "includes": [],
            "nodes": [],
            "warnings": [],
            "errors": [],
            "declared_args": [],
            "declared_args_by_file": {},
            "global_params": [],
            "include_args": {},
            "param_files": [],
            "set_launch_configurations": {},
            "include_deps": [],
            "param_file_deps": [],
            "event_handlers": [],
        }
        self.declared_arg_names: set = set()
        self.namespace_stack: list = []
        self.include_chain: list = []
        self.env: dict = {}
        self.inline_params: bool = False
        self.global_params: list[tuple[str, Any]] = []
        self.global_remaps: list[tuple[str, str]] = []
        self.global_param_files: list[dict] = []
        self.package_shares: dict = {}
        self.apply_opaque_file_access: bool = False
        self.preview_mode: bool = True
        self.lockfile_data: dict = {}
        self.rosdep_fallback: bool = False
        self.apply_arg_defaults: bool = False
        self.global_arg_cascade: bool = False
        self.allow_unportable_paths: bool = False
        self.fetch_dir: str = ""
        self.fetched_packages: set = set()
        self.rosdep_attempted: set = set()
        self.root_source_key: str = ""


_state = ResolverState()


def _is_substitution(value):
    """Return True if *value* is a launch substitution (not yet resolved to a string).

    Handles both single substitution objects (with a ``perform`` method) and
    list-of-substitutions (``SomeSubstitutionsType`` in ROS 2), where any
    element may be a substitution object.
    """
    if value is None or isinstance(value, str):
        return False
    if hasattr(value, "perform"):
        return True
    if isinstance(value, (list, tuple)):
        return any(hasattr(item, "perform") for item in value)
    return False


def _track_package(pkg):
    if not pkg:
        return
    # Skip substitution objects (e.g. LaunchConfiguration) — the variable name
    # is NOT a real package.  The resolved value will be tracked later in
    # _resolve_node_details / _resolve_composable_plugins.
    if _is_substitution(pkg):
        return
    pkg = str(pkg)
    if pkg and pkg not in _state.tracked["packages"]:
        _state.tracked["packages"].append(pkg)


def _extract_pkg_and_share_path(path_str: str):
    """Extract (package, share_path) from a portable or AMENT install path.

    Handles two forms:
      - Portable: ``$(find-pkg-share pkg)/launch/foo.py`` → ``("pkg", "launch/foo.py")``
      - AMENT:    ``/opt/.../share/pkg/launch/foo.py``    → ``("pkg", "launch/foo.py")``

    Returns ``None`` if neither form matches.
    """
    parsed = _parse_portable_path(path_str)
    if parsed:
        return parsed
    # AMENT install path: .../share/<package>/<rest>
    idx = path_str.find("/share/")
    if idx >= 0 and "$(" not in path_str:
        after_share = path_str[idx + 7 :]  # skip "/share/"
        slash = after_share.find("/")
        if slash > 0:
            pkg = after_share[:slash]
            rest = after_share[slash + 1 :]
            if rest:
                return pkg, rest
    return None


def _track_include(path):
    if not path:
        return
    path = str(path)
    if path not in _state.tracked["includes"]:
        _state.tracked["includes"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {
            "package": dep[0],
            "share_path": dep[1],
            "path": path,
            "namespace_stack": list(_state.namespace_stack),
        }
        # Don't deduplicate: the same file may be included multiple times under
        # different <push-ros-namespace> contexts, and each entry carries a distinct
        # namespace_stack that the orchestrator needs for correct namespace propagation.
        entry["include_args"] = {}
        _state.tracked["include_deps"].append(entry)
    # Return the index of the last entry with this path so callers can
    # attach include_args to the correct entry.
    for i in range(len(_state.tracked["include_deps"]) - 1, -1, -1):
        if _state.tracked["include_deps"][i].get("path") == path:
            return i
    return -1


def _track_param_file(path):
    if not path:
        return
    path = str(path)
    if path not in _state.tracked["param_files"]:
        _state.tracked["param_files"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {"package": dep[0], "share_path": dep[1]}
        if entry not in _state.tracked["param_file_deps"]:
            _state.tracked["param_file_deps"].append(entry)


def _current_source_key() -> str:
    """Return the source key for the current file being resolved.

    Uses the last entry of _state.include_chain, or _state.root_source_key for root-level.
    Format: "pkg://share_path" (matching Rust convention).
    """
    if _state.include_chain:
        pkg, path = _state.include_chain[-1]
        return f"{pkg}://{path}" if pkg else path
    return _state.root_source_key


def _portable_display(sub) -> str:
    """Get the portable display string for a substitution without triggering resolution.

    Unlike ``str(sub)`` which may call ``_resolve_pkg_share()`` in non-preview
    mode, this always returns the portable form (e.g. ``$(find-pkg-share pkg)``).
    Used for recording unresolved declared arg defaults in --show-args metadata.
    """
    if isinstance(sub, _TrackedFindPackageShare):
        pkg, _ = sub._resolve_name(None)
        return f"$(find-pkg-share {pkg})"
    if isinstance(sub, _TrackedPathJoinSubstitution):
        return "/".join(_portable_display(s) for s in sub._subs)
    return str(sub)


def _record_declared_arg(name: str, default: str, *, flat: bool = True) -> None:
    """Record a declared arg in the per-file dict, and optionally the flat list.

    The flat list is used by Rust for apply_arg_defaults (first-declaration wins).
    The per-file dict is used by --show-args to render arg comments per included file.
    """
    if flat:
        _state.tracked["declared_args"].append({"name": name, "default": default})
    key = _current_source_key()
    if key:
        by_file = _state.tracked["declared_args_by_file"]
        if key not in by_file:
            by_file[key] = []
        by_file[key].append({"name": name, "default": default})


def _track_node(node_dict: dict) -> int:
    """Append a node dict to _state.tracked["nodes"] with source info from _state.include_chain.

    Returns the index of the appended node.
    """
    if _state.include_chain:
        node_dict["include_chain"] = list(_state.include_chain)
    idx = len(_state.tracked["nodes"])
    _state.tracked["nodes"].append(node_dict)
    return idx


def _track_event_handler(eh_dict: dict) -> int:
    """Track an event handler as a node entry so it appears in encounter order.

    Wraps the event handler dict in a node-shaped dict with ``kind: "event_handler"``
    and uses ``_track_node()`` to get proper ``include_chain`` and ordering.
    """
    node_dict = {
        "package": "",
        "executable": "",
        "name": "",
        "namespace_stack": eh_dict.get("namespace_stack", []),
        "explicit_namespace": eh_dict.get("explicit_namespace"),
        "parameters": {},
        "param_files": [],
        "remappings": [],
        "env": {},
        "kind": "event_handler",
        "plugins": [],
        "target": eh_dict.get("target"),
        # Event-handler-specific fields
        "handler_kind": eh_dict.get("handler_kind", ""),
        "target_node": eh_dict.get("target_node"),
        "start_state": eh_dict.get("start_state"),
        "goal_state": eh_dict.get("goal_state"),
        "eh_actions": eh_dict.get("actions", []),
    }
    return _track_node(node_dict)


def _to_str(value: object, context: Any = None) -> str | None:
    """Coerce a str, substitution object, or None to ``str | None``.

    - ``None`` → ``None`` (caller decides how to handle missing values)
    - ``str``  → returned as-is
    - object with ``.perform()`` → call it; ``None`` result stays ``None``,
      non-``None`` result is coerced to ``str``; on exception → ``str(value)``
    - anything else → ``str(value)``

    Every non-``None`` return value is guaranteed to be ``str``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "perform"):
        try:
            res = value.perform(context)
            return str(res) if res is not None else None
        except _PackageNotFetchedError:
            raise
        except Exception:
            return str(value)
    return str(value)


def _resolve_substitution(sub: object, context: Any) -> str | None:
    """Resolve a substitution, list-of-substitutions, or plain string to str."""
    value, _fallback = _resolve_substitution_ex(sub, context)
    return value


def _resolve_substitution_ex(sub: object, context: Any) -> tuple[str | None, bool]:
    """Resolve a substitution and report whether the result is a fallback.

    Returns ``(resolved_str_or_None, is_fallback)`` where *is_fallback* is
    ``True`` when ``perform()`` returned ``None`` or raised and the display
    name was used instead.  Callers that need to distinguish "really resolved"
    from "fell back to variable name" (e.g. package tracking) should use this.
    """
    if sub is None:
        return None, False
    if isinstance(sub, str):
        # Plain strings from Python launch code are already resolved (they come from
        # Python expressions, not unresolved XML/YAML text).  Do NOT call
        # _resolve_ros_substitutions here — that would eagerly expand portable
        # $(find-pkg-share ...) paths back to machine-specific filesystem paths.
        return sub, False
    if isinstance(sub, (list, tuple)):
        parts = []
        any_fallback = False
        for s in sub:
            if hasattr(s, "perform"):
                if context is not None:
                    try:
                        result = s.perform(context)
                        if result is not None:
                            parts.append(str(result))
                        else:
                            parts.append(str(s))
                            any_fallback = True
                    except _PackageNotFetchedError:
                        raise
                    except Exception:
                        parts.append(str(s))
                        any_fallback = True
                else:
                    parts.append(str(s))
                    any_fallback = True
            else:
                parts.append(str(s))
        return "".join(parts), any_fallback
    if context is not None and hasattr(sub, "perform"):
        try:
            result = sub.perform(context)
            if result is None:
                return str(sub), True  # unresolved — use display name
            return str(result), False
        except _PackageNotFetchedError:
            raise
        except Exception:
            return str(sub), True
    return str(sub), True


def _track_node_from_action(package, executable, name=None):
    """Track a node from an unpatched ROS 2 action (e.g. from OpaqueFunction return)."""
    # Pass raw package to _track_package BEFORE stringifying — _track_package
    # has an _is_substitution guard that filters out substitution objects.
    _track_package(package)
    package = str(package) if package else ""
    executable = str(executable) if executable else ""
    name = str(name) if name else ""
    return _track_node(
        {
            "package": package,
            "executable": executable,
            "name": name,
            "namespace_stack": [],
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "node",
            "plugins": [],
            "target": None,
        }
    )


def _warn(msg: str) -> None:
    _state.tracked["warnings"].append(msg)


def _error(msg: str) -> None:
    _state.tracked["errors"].append(msg)


# ─── XML/YAML Launch File Parser ──────────────────────────────────────────────
#
# Produces a list of element dicts matching the Rust LaunchElement schema.
# Each element is a single-key dict: {"Arg": {...}}, {"Node": {...}}, etc.
# Substitutions in attribute values are left as raw strings for the resolver
# (Phase 3) to process — the parser does not resolve them.
#
# Parsing is delegated to Entity-based parsers in ``launch_plus.parsers``.
# Resolution is dispatched through the action registry via _resolve_element().

import xml.etree.ElementTree as ET  # noqa: F401 — still used by callers

from launch_plus.entities.expose import action_parse_methods, expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.parsers.xml_parser import parse_xml_launch as _parse_xml_launch_entity
from launch_plus.parsers.yaml_parser import parse_yaml_launch as _parse_yaml_launch_entity


def parse_xml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse an XML launch file to a list of Entity objects."""
    return list(_parse_xml_launch_entity(content, file_path))


def parse_yaml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse a YAML launch file to a list of Entity objects."""
    return list(_parse_yaml_launch_entity(content, file_path))


# ─── Substitution Engine (for XML/YAML resolution) ───────────────────────────
#
# Parses and resolves ROS 2 substitution syntax: $(arg x), $(env Y default),
# $(find-pkg-share pkg), $(var x), $(dirname), $(eval expr), $(command ...).
# Used by the XML/YAML AST walker (Phase 3) — Python launch files use the
# existing .perform() mechanism instead.


class _SubstitutionContext:
    """Context for resolving substitutions in XML/YAML launch files."""

    __slots__ = (
        "args",
        "vars",
        "env",
        "launch_file_dir",
        "preview_mode",
    )

    def __init__(self) -> None:
        self.args: dict[str, str] = {}
        self.vars: dict[str, str] = {}
        self.env: dict[str, str] = {}
        self.launch_file_dir: str | None = None
        self.preview_mode: bool = False


def parse_substitutions(text: str) -> list[tuple[str, ...] | str]:
    """Tokenize a string containing ``$(...)`` substitutions.

    Returns a list of tokens:
    - ``str`` for literal text
    - ``tuple`` for substitutions: ``("arg", "name")``, ``("env", "NAME", "default")``, etc.

    Supports nested substitutions via depth counting.
    """
    parts: list[tuple[str, ...] | str] = []
    i = 0
    literal: list[str] = []

    while i < len(text):
        if text[i] == "$" and i + 1 < len(text) and text[i + 1] == "(":
            # Flush literal
            if literal:
                parts.append("".join(literal))
                literal = []
            i += 2  # skip $(
            # Read until matching )
            depth = 1
            expr_chars = []
            while i < len(text):
                c = text[i]
                if c == "(":
                    depth += 1
                    expr_chars.append(c)
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                    expr_chars.append(c)
                else:
                    expr_chars.append(c)
                i += 1
            expr = "".join(expr_chars).strip()
            parts.append(_parse_substitution_expr(expr))
        else:
            literal.append(text[i])
            i += 1

    if literal:
        parts.append("".join(literal))

    return parts


def _parse_substitution_expr(expr: str) -> tuple[str, ...]:
    """Parse the inside of ``$(...)`` into a typed tuple."""
    if expr == "dirname":
        return ("dirname",)

    # Split into command and argument(s)
    parts = expr.split(None, 1)
    cmd = parts[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else None

    if cmd == "arg":
        return ("arg", arg or "")
    if cmd == "var":
        return ("var", arg or "")
    if cmd == "env":
        if arg is None:
            return ("env", "", None)  # type: ignore[return-value]
        env_parts = arg.split(None, 1)
        name = env_parts[0]
        default = env_parts[1].strip() if len(env_parts) > 1 else None
        return ("env", name, default)  # type: ignore[return-value]
    if cmd == "find-pkg-share":
        return ("find-pkg-share", arg or "")
    if cmd == "find-pkg-prefix":
        return ("find-pkg-prefix", arg or "")
    if cmd == "eval":
        return ("eval", _normalize_eval_expr(arg or ""))
    if cmd == "command":
        return ("command", arg or "")

    _warn(f"unknown substitution: $({expr})")
    return ("unknown", expr)


def _normalize_eval_expr(expr: str) -> str:
    """Strip outer quote wrappers from $(eval ...) expressions.

    Handles three quoting styles from XML:
    - Style B: ``'expr'`` with escaped inner quotes
    - Style A: ``"expr"`` (from XML ``&quot;`` entities)
    - Bare expression (no change)
    """

    def _has_unescaped(s: str, ch: str) -> bool:
        i = 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s):
                i += 2
            elif s[i] == ch:
                return True
            else:
                i += 1
        return False

    # Style B: outer ' wrapper
    if (
        len(expr) >= 2
        and expr[0] == "'"
        and expr[-1] == "'"
        and not _has_unescaped(expr[1:-1], "'")
    ):
        expr = expr[1:-1]

    # Unescape \' and \"
    expr = expr.replace("\\'", "'").replace('\\"', '"')

    # Style A: outer " wrapper
    if (
        len(expr) >= 2
        and expr[0] == '"'
        and expr[-1] == '"'
        and not _has_unescaped(expr[1:-1], '"')
    ):
        expr = expr[1:-1]

    return expr


def resolve_substitutions_from_tokens(
    tokens: "list[_SubstitutionType]",
    ctx: _SubstitutionContext,
    *,
    _depth: int = 0,
) -> str:
    """Resolve a pre-parsed list of :class:`Substitution` objects to a string.

    Each token's ``.perform(ctx)`` is called in order and the results
    are concatenated.  This is the low-level entry point used by
    individual substitution implementations when they need to recursively
    resolve nested tokens.
    """
    if _depth > 50:
        _error("substitution recursion limit exceeded")
        return "".join(t.serialize() for t in tokens)
    return "".join(t.perform(ctx, _depth=_depth) for t in tokens)


def resolve_substitutions(
    text: str,
    ctx: _SubstitutionContext,
    _depth: int = 0,
) -> str:
    """Resolve all substitutions in a string using the given context.

    Parses *text* via the Lark grammar into typed :class:`Substitution`
    objects, then calls ``.perform()`` on each to produce the resolved
    string.  Falls back to the legacy depth-counting tokenizer for
    expressions that the grammar cannot handle (e.g. ``$(eval ...)``
    with Python operators outside quotes).
    """
    if _depth > 50:
        _error(f"substitution recursion limit exceeded: {text[:100]}")
        return text
    from launch_plus.parsers.parse_substitution import parse_substitution as _lark_parse

    try:
        tokens = _lark_parse(text)
    except Exception:
        # Lark grammar is stricter than the original depth-counting parser.
        # Fall back to the legacy tokenizer for inputs with non-standard syntax
        # (e.g. $(eval '$(var x)'=='y') mixing Python operators in substitutions).
        return _resolve_substitutions_legacy(text, ctx, _depth)
    return resolve_substitutions_from_tokens(tokens, ctx, _depth=_depth)


def _resolve_substitutions_legacy(
    text: str,
    ctx: _SubstitutionContext,
    _depth: int = 0,
) -> str:
    """Legacy fallback resolver using the depth-counting tokenizer.

    Used when the Lark grammar cannot parse certain edge-case expressions
    (e.g. ``$(eval '$(var x)'=='y')``).
    """
    if _depth > 50:
        _error(f"substitution recursion limit exceeded: {text[:100]}")
        return text
    tokens = parse_substitutions(text)
    parts: list[str] = []

    for token in tokens:
        if isinstance(token, str):
            parts.append(token)
            continue

        kind = token[0]

        if kind == "arg":
            name = token[1]
            name = resolve_substitutions(name, ctx, _depth + 1)
            value = ctx.args.get(name)
            if value is None:
                _error(f"undefined argument: {name}")
                parts.append(f"$(arg {name})")
            else:
                parts.append(resolve_substitutions(value, ctx, _depth + 1))

        elif kind == "var":
            name = token[1]
            name = resolve_substitutions(name, ctx, _depth + 1)
            if name in ctx.vars:
                value = ctx.vars[name]
            elif name in ctx.args:
                value = ctx.args[name]
            else:
                value = None
            if value is None:
                _error(f"undefined variable: {name}")
                parts.append(f"$(var {name})")
            else:
                parts.append(resolve_substitutions(value, ctx, _depth + 1))

        elif kind == "env":
            name = token[1]
            default = token[2] if len(token) > 2 else None
            value = ctx.env.get(name)
            if value is None:
                value = os.environ.get(name)
            if value is None:
                value = default  # type: ignore[assignment]
            if value is None:
                _error(f"environment variable not set: {name}")
                parts.append(f"$(env {name})")
            else:
                parts.append(str(value))

        elif kind == "find-pkg-share":
            pkg = token[1]
            pkg = resolve_substitutions(pkg, ctx, _depth + 1)
            _track_package(pkg)
            if ctx.preview_mode:
                parts.append(f"$(find-pkg-share {pkg})")
            else:
                try:
                    path = _resolve_pkg_share(pkg)
                    parts.append(path)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    parts.append(f"$(find-pkg-share {pkg})")

        elif kind == "find-pkg-prefix":
            pkg = token[1]
            pkg = resolve_substitutions(pkg, ctx, _depth + 1)
            _track_package(pkg)
            parts.append(f"$(find-pkg-prefix {pkg})")

        elif kind == "dirname":
            if ctx.launch_file_dir:
                parts.append(ctx.launch_file_dir)
            else:
                parts.append("$(dirname)")

        elif kind == "eval":
            expr = token[1]
            expr = resolve_substitutions(expr, ctx, _depth + 1)
            expr = expr.replace("\\'", "'").replace('\\"', '"')
            try:
                result = eval(expr)  # noqa: S307
                parts.append(str(result))
            except Exception as e:
                _error(f"$(eval {expr}) failed: {e}")
                if ctx.preview_mode:
                    parts.append(f"$(eval {expr})")

        elif kind == "command":
            body = token[1]
            body = resolve_substitutions(body, ctx, _depth + 1)
            parts.append(f"$(command {body})")

        else:
            parts.append(f"$({' '.join(token)})")

    return "".join(parts)


# ─── XML/YAML AST Walker (Phase 3) ──────────────────────────────────────────
#
# Walks the element list produced by parse_xml_launch / parse_yaml_launch,
# resolves substitutions, evaluates conditions, and populates _state.tracked.
# This is the XML/YAML counterpart of the Python _walk_actions mechanism.


def _is_truthy(value: str) -> bool:
    """Check if a resolved condition value is truthy (ROS 2 convention)."""
    return value.strip().lower() in ("true", "1", "yes", "on")


def _evaluate_condition(
    condition: dict[str, str] | None,
    ctx: _SubstitutionContext,
) -> bool:
    """Evaluate an if/unless condition dict.  Returns True if the element should execute."""
    if condition is None:
        return True
    kind = condition["kind"]
    expr = condition["expr"]
    resolved = resolve_substitutions(expr, ctx)
    truthy = _is_truthy(resolved)
    if kind == "If":
        return truthy
    # Unless
    return not truthy


def _ros2_namespace_join(base: str | None, next_ns: str) -> str | None:
    """Join two ROS 2 namespace components.  Matches Rust effective_namespace."""
    next_ns = next_ns.rstrip("/")
    if not next_ns:
        return base
    if next_ns.startswith("/"):
        # Absolute — resets
        return next_ns
    if not base or base in ("", "/"):
        return f"/{next_ns}"
    return f"{base.rstrip('/')}/{next_ns}"


def _effective_namespace(
    stack: list[str],
    explicit_ns: str | None = None,
) -> str | None:
    """Compute effective namespace from stack + optional node-level namespace."""
    current: str | None = None
    for component in stack:
        current = _ros2_namespace_join(current, component)
    if explicit_ns:
        current = _ros2_namespace_join(current, explicit_ns)
    return current


def _expand_ros_params_yaml(content: str) -> list[tuple[str, str]]:
    """Parse ROS 2 parameter YAML and flatten into (key, value) pairs.

    Supports all standard ROS 2 layouts:
      - bare ``ros__parameters: ...``
      - ``/**:\\n  ros__parameters: ...`` (Autoware wildcard convention)
      - ``/ns:\\n  node_name:\\n    ros__parameters: ...`` (general ROS 2)
    """
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    _collect_ros_params(data, 0, out)
    return out


def _collect_ros_params(value: object, depth: int, out: list[tuple[str, str]]) -> None:
    if depth > 3 or not isinstance(value, dict):
        return
    if "ros__parameters" in value:
        _flatten_yaml_value(value["ros__parameters"], "", out)
    else:
        for child in value.values():
            if isinstance(child, dict):
                _collect_ros_params(child, depth + 1, out)


def _flatten_yaml_value(value: object, prefix: str, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            full_key = f"{prefix}.{k}" if prefix else str(k)
            _flatten_yaml_value(v, full_key, out)
    elif isinstance(value, list):
        items = ", ".join(_yaml_value_to_str(v) for v in value)
        out.append((prefix, f"[{items}]"))
    else:
        out.append((prefix, _yaml_value_to_str(value)))


def _yaml_value_to_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, list):
        items = ", ".join(_yaml_value_to_str(x) for x in v)
        return f"[{items}]"
    return str(v)


def _read_and_expand_param_file(
    path: str,
    ctx: "_SubstitutionContext | None" = None,
) -> list[tuple[str, str]] | None:
    """Read a param file and expand ros__parameters. Returns None on failure."""
    real_path = path
    parsed = _parse_portable_path(path)
    if parsed:
        pkg, rest = parsed
        pkg_share = _state.package_shares.get(pkg)
        if not pkg_share:
            # Try fetching the package if it's in the lockfile.
            if _ensure_fetched(pkg):
                pkg_share = _state.package_shares.get(pkg)
            if not pkg_share:
                try:
                    pkg_share = _resolve_pkg_share(pkg)
                except Exception:
                    _error(f"param file not found: '{path}' (package not available)")
                    return None
        real_path = os.path.join(pkg_share, rest)
    if not os.path.isfile(real_path):
        # Package share was known but file missing — try full fetch.
        if parsed and _ensure_fetched(parsed[0]):
            pkg_share = _state.package_shares.get(parsed[0])
            if pkg_share:
                real_path = os.path.join(pkg_share, parsed[1])
        if not os.path.isfile(real_path):
            _error(f"param file not found: '{real_path}' (resolved from '{path}')")
            return None
    try:
        with open(real_path) as f:
            content = f.read()
        pairs = _expand_ros_params_yaml(content)
        if pairs and ctx is not None:
            resolved_pairs: list[tuple[str, str]] = []
            for key, val in pairs:
                try:
                    resolved_val = resolve_substitutions(val, ctx)
                except Exception:
                    resolved_val = val
                resolved_pairs.append((key, resolved_val))
            return resolved_pairs
        return pairs
    except Exception as e:
        _error(f"--inline-params: failed to read '{path}': {e}")
        return None


def _make_param_file_entry(path: str) -> dict:
    """Create a param_file dict entry, with optional inline expansion."""
    pf_entry: dict = {"path": path}
    if _state.inline_params:
        expanded = _read_and_expand_param_file(path)
        if expanded is not None:
            pf_entry["params"] = expanded
    return pf_entry


def resolve_xml_elements(
    elements: list[Entity],
    ctx: _SubstitutionContext,
    *,
    include_stack: list[str] | None = None,
) -> None:
    """Walk parsed XML/YAML elements, resolve substitutions, populate _state.tracked.

    This is the XML/YAML counterpart of the Python ``_walk_actions`` mechanism.
    All output goes into the module-level ``_state.tracked`` dict, ``_state.namespace_stack``,
    and ``_state.env``, matching the same format the Rust orchestrator expects.

    Dispatches each element through the action registry via :func:`_resolve_element`.
    """
    if include_stack is None:
        include_stack = []
    for elem in elements:
        _resolve_element(elem, ctx, include_stack)


def _resolve_element(
    elem: Entity,
    ctx: _SubstitutionContext,
    include_stack: list[str],
) -> None:
    """Resolve a single parsed element via the action registry."""
    tag = elem.type_name
    if tag in action_parse_methods:
        parser = _ActionParser(ctx, include_stack)
        action_parse_methods[tag](elem, parser)
        return
    _warn(f"unknown element: <{tag}>")


# ── ActionParser — resolver services for action handlers ─────────────────────


class _ActionParser:
    """Provides resolver services to action handlers.

    Wraps the module-level resolver state so action handlers can
    access substitution resolution, condition evaluation, namespace
    management, and tracking without importing resolver internals.
    """

    __slots__ = ("ctx", "include_stack")

    def __init__(self, ctx: _SubstitutionContext, include_stack: list[str]) -> None:
        self.ctx = ctx
        self.include_stack = include_stack

    def resolve(self, text: str) -> str:
        """Resolve ``$(...)`` substitutions in *text*."""
        return resolve_substitutions(text, self.ctx)

    def resolve_optional(self, text: str | None) -> str | None:
        if text is None:
            return None
        return resolve_substitutions(text, self.ctx)

    def evaluate_condition(self, entity: Entity) -> bool:
        """Evaluate if=/unless= on *entity*.  Returns True → element should execute."""
        if_val = entity.get_attr("if", optional=True)
        unless_val = entity.get_attr("unless", optional=True)
        cond: dict[str, str] | None = None
        if if_val is not None:
            cond = {"kind": "If", "expr": if_val}
        elif unless_val is not None:
            cond = {"kind": "Unless", "expr": unless_val}
        return _evaluate_condition(cond, self.ctx)

    def resolve_children(self, entities: list[Entity]) -> None:
        """Resolve child entities recursively for side-effects."""
        for child in entities:
            _resolve_element(child, self.ctx, self.include_stack)

    def effective_namespace(self, ns: str | None = None) -> str | None:
        return _effective_namespace(list(_state.namespace_stack), ns)

    # ── Tracking helpers ──────────────────────────────────────────────

    def track_node(self, node_dict: dict) -> int:
        """Delegate to module-level ``_track_node``."""
        return _track_node(node_dict)

    @property
    def namespace_stack(self) -> list:
        return _state.namespace_stack

    @property
    def env(self) -> dict:
        return _state.env

    def push_include_chain(self, file_path: str) -> None:
        inc_dep = _extract_pkg_and_share_path(file_path)
        if inc_dep:
            _state.include_chain.append(list(inc_dep))
        else:
            _state.include_chain.append(["", file_path])

    def pop_include_chain(self) -> None:
        _state.include_chain.pop()

    # ── Param / remap / env resolution from Entity children ──────────

    def resolve_params(self, entity: Entity) -> tuple[dict[str, str], list[dict]]:
        """Resolve <param> children.  Returns (params_dict, param_files_list)."""
        items = entity.get_attr("param", data_type=list, optional=True)
        if not items:
            return {}, []
        resolved: dict[str, str] = {}
        param_files: list[dict] = []
        seen_paths: set[str] = set()
        for p in items:
            name = p.get_attr("name", optional=True)
            value = p.get_attr("value", optional=True)
            from_file = p.get_attr("from", optional=True)
            if from_file:
                path = self.resolve(from_file)
                _track_param_file(path)
                if path not in seen_paths:
                    seen_paths.add(path)
                    pf_entry: dict = {"path": path}
                    if _state.inline_params:
                        expanded = _read_and_expand_param_file(path, self.ctx)
                        if expanded is not None:
                            pf_entry["params"] = expanded
                    param_files.append(pf_entry)
            elif name:
                resolved[self.resolve(name)] = self.resolve(value or "")
        return resolved, param_files

    def resolve_remaps(self, entity: Entity) -> list[list[str]]:
        """Resolve <remap> children."""
        items = entity.get_attr("remap", data_type=list, optional=True)
        if not items:
            return []
        return [
            [
                self.resolve(r.get_attr("from", optional=True) or ""),
                self.resolve(r.get_attr("to", optional=True) or ""),
            ]
            for r in items
        ]

    def resolve_envs(self, entity: Entity) -> dict[str, str]:
        """Resolve <env> children into a dict."""
        items = entity.get_attr("env", data_type=list, optional=True)
        if not items:
            return {}
        return {
            self.resolve(e.get_attr("name", optional=True) or ""): self.resolve(
                e.get_attr("value", optional=True) or ""
            )
            for e in items
        }

    def resolve_composable_plugins(self, entity: Entity) -> list[dict]:
        """Resolve <composable_node> children into plugin dicts."""
        items = entity.get_attr("composable_node", data_type=list, optional=True)
        if not items:
            return []
        plugins: list[dict] = []
        for cn in items:
            cond_if = cn.get_attr("if", optional=True)
            cond_unless = cn.get_attr("unless", optional=True)
            cond: dict[str, str] | None = None
            if cond_if is not None:
                cond = {"kind": "If", "expr": cond_if}
            elif cond_unless is not None:
                cond = {"kind": "Unless", "expr": cond_unless}
            if not _evaluate_condition(cond, self.ctx):
                continue
            pkg = self.resolve(cn.get_attr("pkg", optional=True) or "")
            plugin_name = self.resolve(cn.get_attr("plugin", optional=True) or "")
            name = self.resolve_optional(cn.get_attr("name", optional=True))
            _track_package(pkg)
            sub = _ActionParser(self.ctx, self.include_stack)
            params, param_files = sub.resolve_params(cn)
            remaps = sub.resolve_remaps(cn)
            plugins.append(
                {
                    "package": pkg,
                    "plugin": plugin_name,
                    "name": name,
                    "parameters": params,
                    "remappings": remaps,
                    "param_files": param_files,
                }
            )
        return plugins

    # ── Include support ──────────────────────────────────────────────

    def resolve_include_args(self, entity: Entity) -> dict[str, str]:
        """Resolve <arg> children inside an <include>, sequentially."""
        items = entity.get_attr("arg", data_type=list, optional=True)
        if not items:
            return {}
        child_ctx_args: dict[str, str] = {}
        for a in items:
            arg_name = a.get_attr("name", optional=True) or ""
            arg_value = a.get_attr("value", optional=True)
            if arg_value is not None:
                tmp_ctx = _SubstitutionContext()
                tmp_ctx.args = {**self.ctx.args, **child_ctx_args}
                tmp_ctx.vars = dict(self.ctx.vars)
                tmp_ctx.env = dict(self.ctx.env)
                tmp_ctx.launch_file_dir = self.ctx.launch_file_dir
                tmp_ctx.preview_mode = self.ctx.preview_mode
                child_ctx_args[arg_name] = resolve_substitutions(arg_value, tmp_ctx)
        return child_ctx_args

    def parse_and_resolve_included_file(
        self, real_path: str, file_path: str, child_ctx_args: dict[str, str]
    ) -> None:
        """Parse an included launch file and resolve it recursively."""
        self.push_include_chain(file_path)
        new_stack = self.include_stack + [file_path]
        try:
            if real_path.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
                with open(real_path) as f:
                    content = f.read()
                child_entities: list[Entity]
                if real_path.endswith((".yaml", ".yml")):
                    child_entities = list(_parse_yaml_launch_entity(content, real_path))
                else:
                    child_entities = list(_parse_xml_launch_entity(content, real_path))
                child_ctx = _SubstitutionContext()
                if _state.global_arg_cascade:
                    child_ctx.args = {**self.ctx.args, **child_ctx_args}
                    child_ctx.vars = {**self.ctx.vars, **child_ctx_args}
                else:
                    child_ctx.args = dict(child_ctx_args)
                    child_ctx.vars = dict(child_ctx_args)
                child_ctx.env = dict(self.ctx.env)
                child_ctx.launch_file_dir = os.path.dirname(real_path)
                child_ctx.preview_mode = self.ctx.preview_mode
                for child in child_entities:
                    _resolve_element(child, child_ctx, new_stack)
                self.ctx.args.update(child_ctx.args)
                self.ctx.vars.update(child_ctx.vars)
            elif real_path.endswith((".launch.py", ".py")):
                parent_lc = _make_launch_context({**self.ctx.args, **self.ctx.vars})
                if _state.global_params:
                    parent_lc._launch_configurations["global_params"] = list(_state.global_params)
                _inline_resolve_python_launch(
                    file_path, parent_lc, child_ctx_args, len(self.include_stack) + 1
                )
                set_configs = _state.tracked["set_launch_configurations"]
                for k, v in parent_lc._launch_configurations.items():
                    if (k in set_configs or k in child_ctx_args) and k != "global_params":
                        self.ctx.vars[k] = str(v) if not isinstance(v, str) else v
        finally:
            self.pop_include_chain()


# ── Registered action handlers ────────────────────────────────────────────────
#
# Each handler reads from Entity via get_attr(), resolves substitutions via
# the parser, and populates _state.tracked directly.  The @expose_action wrapper
# validates that all entity attributes were consumed.


@expose_action("arg")
def _action_arg(entity: Entity, parser: _ActionParser) -> None:
    name = entity.get_attr("name", optional=True) or ""
    default = entity.get_attr("default", optional=True)
    fixed_value = entity.get_attr("value", optional=True)
    _ = entity.get_attr("description", optional=True)  # consume
    ctx = parser.ctx
    if fixed_value is not None:
        resolved = parser.resolve(fixed_value)
        ctx.args[name] = resolved
    elif name and name not in ctx.args and default is not None:
        if _state.apply_arg_defaults:
            resolved = parser.resolve(default)
            ctx.args[name] = resolved
        else:
            resolved = default or ""
    else:
        resolved = ctx.args.get(name, default or "")
    if name:
        already_seen = name in _state.declared_arg_names
        if not already_seen:
            _state.declared_arg_names.add(name)
        _record_declared_arg(name, resolved, flat=not already_seen)


@expose_action("let")
def _action_let(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = entity.get_attr("name", optional=True) or ""
        value = parser.resolve(entity.get_attr("value", optional=True) or "")
        parser.ctx.vars[name] = value


@expose_action("group")
def _action_group(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    scoped_raw = entity.get_attr("scoped", optional=True)
    if scoped_raw is None:
        scoped = True
    elif isinstance(scoped_raw, bool):
        scoped = scoped_raw
    else:
        scoped = str(scoped_raw).lower() not in ("false", "0", "no")
    children = entity.children
    if scoped:
        saved_args = dict(parser.ctx.args)
        saved_vars = dict(parser.ctx.vars)
        saved_env = dict(_state.env)
        saved_ns_depth = len(_state.namespace_stack)
        saved_gp = list(_state.global_params)
        saved_gr = list(_state.global_remaps)
        saved_gpf = list(_state.global_param_files)
    parser.resolve_children(list(children))
    if scoped:
        new_args = {k: v for k, v in parser.ctx.args.items() if k not in saved_args}
        parser.ctx.args = saved_args
        parser.ctx.args.update(new_args)
        parser.ctx.vars = saved_vars
        _state.env = saved_env
        del _state.namespace_stack[saved_ns_depth:]
        _state.global_params[:] = saved_gp
        _state.global_remaps[:] = saved_gr
        _state.global_param_files[:] = saved_gpf


@expose_action("include")
def _action_include(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    raw_file = entity.get_attr("file", optional=True) or ""
    file_path = parser.resolve(raw_file)

    # Check for unportable absolute paths in preview mode
    if (
        _state.preview_mode
        and os.path.isabs(file_path)
        and "$(find-pkg-share" not in raw_file
        and "$(dirname)" not in raw_file
    ):
        if _state.allow_unportable_paths:
            _warn(f"unportable absolute path in include: {file_path}")
        else:
            _error(f"unportable absolute path in include: {file_path}")

    if file_path in parser.include_stack:
        _error(f"circular include detected: {file_path}")
        return
    if len(parser.include_stack) > 20:
        _warn(f"max include depth exceeded for {file_path}")
        return
    dep_idx = _track_include(file_path)
    child_ctx_args = parser.resolve_include_args(entity)
    if dep_idx >= 0 and child_ctx_args:
        _state.tracked["include_deps"][dep_idx]["include_args"] = child_ctx_args
    if child_ctx_args:
        _state.tracked["include_args"][file_path] = child_ctx_args
    real_path = file_path
    parsed_path = _parse_portable_path(file_path)
    if parsed_path:
        pkg, rest = parsed_path
        try:
            pkg_share = _resolve_pkg_share(pkg)
            real_path = os.path.join(pkg_share, rest)
        except _PackageNotFetchedError:
            raise
        except Exception:
            return
    if os.path.isfile(real_path):
        parser.parse_and_resolve_included_file(real_path, file_path, child_ctx_args)


@expose_action("node")
@expose_action("lifecycle_node")
def _action_node(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    pkg = parser.resolve(
        entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
    )
    exe = parser.resolve(
        entity.get_attr("exec", optional=True) or entity.get_attr("executable", optional=True) or ""
    )
    name = parser.resolve_optional(entity.get_attr("name", optional=True))
    ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    _track_package(pkg)
    params, param_files = parser.resolve_params(entity)
    remaps = parser.resolve_remaps(entity)
    env = dict(_state.env)
    env.update(parser.resolve_envs(entity))
    merged_params = {k: str(v) for k, v in _state.global_params}
    merged_params.update(params)
    merged_param_files = list(_state.global_param_files) + param_files
    merged_remaps = list(_state.global_remaps) + remaps
    node_kind = "node" if entity.type_name != "lifecycle_node" else "lifecycle_node"
    parser.track_node(
        {
            "package": pkg,
            "executable": exe,
            "name": name or "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": ns,
            "parameters": merged_params,
            "param_files": merged_param_files,
            "remappings": merged_remaps,
            "env": env,
            "kind": node_kind,
            "plugins": [],
            "target": None,
            "output": parser.resolve_optional(entity.get_attr("output", optional=True)),
            "args": parser.resolve_optional(entity.get_attr("args", optional=True)),
            "respawn": parser.resolve_optional(entity.get_attr("respawn", optional=True)),
            "respawn_delay": parser.resolve_optional(
                entity.get_attr("respawn_delay", optional=True)
            ),
        }
    )


@expose_action("node_container")
@expose_action("composable_node_container")
def _action_node_container(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    pkg = parser.resolve(
        entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
    )
    exe = parser.resolve(
        entity.get_attr("exec", optional=True) or entity.get_attr("executable", optional=True) or ""
    )
    name = parser.resolve_optional(entity.get_attr("name", optional=True))
    ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    _track_package(pkg)
    env = dict(_state.env)
    env.update(parser.resolve_envs(entity))
    plugins = parser.resolve_composable_plugins(entity)
    parser.track_node(
        {
            "package": pkg,
            "executable": exe,
            "name": name or "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": ns,
            "parameters": {k: str(v) for k, v in _state.global_params},
            "param_files": list(_state.global_param_files),
            "remappings": list(_state.global_remaps),
            "env": env,
            "kind": "container",
            "plugins": plugins,
            "target": None,
        }
    )


@expose_action("load_composable_node")
def _action_load_composable_node(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    target = parser.resolve_optional(entity.get_attr("target", optional=True))
    ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    plugins = parser.resolve_composable_plugins(entity)
    parser.track_node(
        {
            "package": "",
            "executable": "",
            "name": "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": ns,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "load_composable",
            "plugins": plugins,
            "target": target or "",
        }
    )


@expose_action("set_env")
def _action_set_env(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = parser.resolve(entity.get_attr("name", optional=True) or "")
        value = parser.resolve(entity.get_attr("value", optional=True) or "")
        _state.env[name] = value
        parser.ctx.env[name] = value


@expose_action("unset_env")
def _action_unset_env(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = parser.resolve(entity.get_attr("name", optional=True) or "")
        _state.env.pop(name, None)
        parser.ctx.env.pop(name, None)


@expose_action("push-ros-namespace")
def _action_push_ros_namespace(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        ns = parser.resolve(entity.get_attr("namespace", optional=True) or "")
        if ns:
            _state.namespace_stack.append(ns)


@expose_action("set_parameter")
def _action_set_parameter(entity: Entity, parser: _ActionParser) -> None:
    name = parser.resolve(entity.get_attr("name", optional=True) or "")
    value = parser.resolve(entity.get_attr("value", optional=True) or "")
    _state.tracked["global_params"].append([name, value])
    _state.global_params.append((name, value))


@expose_action("set_remap")
def _action_set_remap(entity: Entity, parser: _ActionParser) -> None:
    src = parser.resolve(entity.get_attr("from", optional=True) or "")
    dst = parser.resolve(entity.get_attr("to", optional=True) or "")
    _state.global_remaps.append((src, dst))


@expose_action("log")
def _action_log(entity: Entity, parser: _ActionParser) -> None:
    msg = parser.resolve(entity.get_attr("message", optional=True) or "")
    parser.track_node(
        {
            "package": "",
            "executable": "",
            "name": "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "log",
            "plugins": [],
            "target": None,
            "message": msg,
        }
    )


@expose_action("executable")
def _action_executable(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    cmd = parser.resolve(entity.get_attr("cmd", optional=True) or "")
    name = parser.resolve_optional(entity.get_attr("name", optional=True))
    shell_raw = entity.get_attr("shell", optional=True)
    if shell_raw is None:
        shell = False
    elif isinstance(shell_raw, bool):
        shell = shell_raw
    else:
        shell = str(shell_raw).lower() in ("true", "1", "yes")
    parser.track_node(
        {
            "package": "",
            "executable": "",
            "name": name or "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": dict(_state.env),
            "kind": "executable",
            "plugins": [],
            "target": None,
            "cmd": cmd,
            "shell": shell,
        }
    )


@expose_action("on_process_start")
@expose_action("on_process_exit")
@expose_action("on_state_transition")
@expose_action("on_shutdown")
def _action_event_handler(entity: Entity, parser: _ActionParser) -> None:
    handler_kind = entity.type_name
    target = parser.resolve_optional(entity.get_attr("target", optional=True))
    target_node = parser.resolve_optional(entity.get_attr("target_node", optional=True))
    handler_ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    start_state = parser.resolve_optional(entity.get_attr("start_state", optional=True))
    goal_state = parser.resolve_optional(entity.get_attr("goal_state", optional=True))
    eh_actions: list[dict] = []
    for child in entity.children:
        if child.type_name == "emit_event":
            event = parser.resolve(child.get_attr("event", optional=True) or "")
            ee_target = parser.resolve_optional(child.get_attr("target_node", optional=True))
            ee_ns = parser.resolve_optional(child.get_attr("namespace", optional=True))
            eh_actions.append(
                {
                    "event": event,
                    "target_node": ee_target,
                    "namespace_stack": [],
                    "explicit_namespace": ee_ns,
                }
            )
    _track_event_handler(
        {
            "handler_kind": handler_kind,
            "target": target,
            "target_node": target_node,
            "start_state": start_state,
            "goal_state": goal_state,
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": handler_ns,
            "actions": eh_actions,
        }
    )


@expose_action("emit_event")
def _action_emit_event(entity: Entity, parser: _ActionParser) -> None:
    event = parser.resolve(entity.get_attr("event", optional=True) or "")
    target_node = parser.resolve_optional(entity.get_attr("target_node", optional=True))
    ee_ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    _track_event_handler(
        {
            "handler_kind": "emit_event",
            "target": None,
            "target_node": target_node,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": ee_ns,
            "actions": [
                {
                    "event": event,
                    "target_node": target_node,
                    "namespace_stack": list(_state.namespace_stack),
                    "explicit_namespace": ee_ns,
                }
            ],
        }
    )


# ─── Shim classes ─────────────────────────────────────────────────────────────


class _TrackedNode:
    def __init__(self, *, package=None, executable=None, name=None, **kwargs):
        _track_package(package)
        self._idx = _track_node(
            {
                "package": str(package) if package else "",
                "executable": str(executable) if executable else "",
                "name": str(name) if name else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "node",
                "plugins": [],
                "target": None,
            }
        )
        # Save raw kwargs for deferred resolution in _walk_action
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._raw_output = kwargs.get("output")
        self._raw_arguments = kwargs.get("arguments")
        self._raw_respawn = kwargs.get("respawn")
        self._raw_respawn_delay = kwargs.get("respawn_delay")
        self._detailed = False
        # Eager: track any ParameterFile paths identifiable at construction time
        for p in self._raw_parameters:
            if hasattr(p, "_param_file") and p._param_file:
                _track_param_file(p._param_file)

    def __repr__(self):
        return f"TrackedNode(package={_state.tracked['nodes'][self._idx]['package']!r})"


class _TrackedLifecycleNode(_TrackedNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        _state.tracked["nodes"][self._idx]["kind"] = "lifecycle_node"


class _TrackedEmitEvent:
    """Tracked emit_event — records the event type and optional target node."""

    def __init__(self, event=None, **kwargs):
        self._event = event
        self._target_node = None
        self._namespace_stack = []
        self._explicit_namespace = None
        # Extract target node from ChangeState-like event objects
        if event is not None and hasattr(event, "_target_node"):
            self._target_node = event._target_node
        if event is not None and hasattr(event, "_event_name"):
            self._event = event._event_name
        if event is not None and hasattr(event, "_namespace_stack"):
            self._namespace_stack = event._namespace_stack
            self._explicit_namespace = event._explicit_namespace

    def to_dict(self):
        return {
            "event": str(self._event) if self._event else "",
            "target_node": self._target_node,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
        }


class _TrackedChangeState:
    """Tracked ChangeState event — records the transition and target node.

    Real API: ``ChangeState(lifecycle_node_matcher=matches_action(node), transition_id=...)``
    where ``matches_action(node)`` returns a ``_TrackedMatchesAction`` that carries ``_node_name``.
    """

    def __init__(self, lifecycle_node_matcher=None, transition_id=None, **kwargs):
        self._event_name = _transition_name(transition_id)
        self._target_node = None
        self._namespace_stack = []
        self._explicit_namespace = None
        if lifecycle_node_matcher is not None and hasattr(lifecycle_node_matcher, "_node_name"):
            self._target_node = lifecycle_node_matcher._node_name
            self._namespace_stack = getattr(lifecycle_node_matcher, "_namespace_stack", [])
            self._explicit_namespace = getattr(lifecycle_node_matcher, "_explicit_namespace", None)


class _TrackedShutdown:
    """Tracked Shutdown event.

    When used directly as an action (rather than wrapped in EmitEvent),
    serializes as an emit_event with event="shutdown".
    """

    _event_name = "shutdown"

    def __init__(self, **kwargs):
        if kwargs:
            _error(
                f"Shutdown event arguments are not yet supported: "
                f"{', '.join(f'{k}={v!r}' for k, v in kwargs.items())}"
            )

    def to_dict(self):
        return {
            "event": "shutdown",
            "target_node": None,
            "namespace_stack": [],
            "explicit_namespace": None,
        }


class _TrackedMatchesAction:
    """Wraps ``matches_action(node)`` — carries the node name for ChangeState targeting.

    The real ``launch.events.matches_action`` returns a callable matcher.
    Our version also records the node name and namespace so ``_TrackedChangeState``
    can extract them.
    """

    def __init__(self, action):
        self._node_name = _action_name(action)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(action)

    def __call__(self, *args, **kwargs):
        return True  # dummy matcher


def _transition_name(transition_id):
    """Convert a lifecycle Transition constant to a human-readable name."""
    # Transition IDs are integers in the lifecycle_msgs
    _TRANSITION_MAP = {
        1: "configure",  # TRANSITION_CONFIGURE
        2: "cleanup",  # TRANSITION_CLEANUP
        3: "activate",  # TRANSITION_ACTIVATE
        4: "deactivate",  # TRANSITION_DEACTIVATE
        5: "shutdown",  # TRANSITION_UNCONFIGURED_SHUTDOWN
        6: "shutdown",  # TRANSITION_INACTIVE_SHUTDOWN
        7: "shutdown",  # TRANSITION_ACTIVE_SHUTDOWN
    }
    if isinstance(transition_id, int):
        return _TRANSITION_MAP.get(transition_id, f"transition_{transition_id}")
    return str(transition_id) if transition_id else ""


class _TrackedOnProcessStart:
    """Tracked OnProcessStart event handler."""

    def __init__(self, target_action=None, on_start=None, **kwargs):
        self._target_name = _action_name(target_action)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(target_action)
        self._actions = on_start or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_process_start",
            "target": self._target_name,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnProcessExit:
    """Tracked OnProcessExit event handler."""

    def __init__(self, target_action=None, on_exit=None, **kwargs):
        self._target_name = _action_name(target_action)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(target_action)
        self._actions = on_exit or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_process_exit",
            "target": self._target_name,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnStateTransition:
    """Tracked OnStateTransition event handler.

    Real API: ``OnStateTransition(target_lifecycle_node=node,
    start_state='configuring', goal_state='inactive', entities=[...])``
    """

    def __init__(
        self, target_lifecycle_node=None, start_state=None, goal_state=None, entities=None, **kwargs
    ):
        self._target_node = _action_name(target_lifecycle_node)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(
            target_lifecycle_node
        )
        self._start_state = str(start_state) if start_state else None
        self._goal_state = str(goal_state) if goal_state else None
        self._actions = entities or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_state_transition",
            "target": None,
            "target_node": self._target_node or None,
            "start_state": self._start_state,
            "goal_state": self._goal_state,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnShutdown:
    """Tracked OnShutdown event handler.

    OnShutdown fires when the launch system is shutting down.  It has no
    target process — it applies system-wide.  Rendered as ``<on_shutdown>``.
    """

    def __init__(self, on_shutdown=None, **kwargs):
        self._actions = on_shutdown or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_shutdown",
            "target": None,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": [],
            "explicit_namespace": None,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedRegisterEventHandler:
    """Tracked RegisterEventHandler — records the event handler to _state.tracked."""

    def __init__(self, event_handler=None, **kwargs):
        if event_handler is not None and hasattr(event_handler, "to_event_handler"):
            _track_event_handler(event_handler.to_event_handler())


def _action_name(action):
    """Extract the node name from a tracked action for event handler targeting."""
    if action is not None and hasattr(action, "_idx"):
        return _state.tracked["nodes"][action._idx].get("name", "")
    return ""


def _action_namespace_info(action):
    """Extract namespace_stack and explicit_namespace for a tracked node action.

    Returns (namespace_stack, explicit_namespace) from the tracked entry.
    By the time event handlers are registered, _resolve_node_details has
    already run on the target node (LifecycleNode is processed before
    RegisterEventHandler in _walk_actions), so these fields are populated.
    """
    if action is None or not hasattr(action, "_idx"):
        return [], None
    entry = _state.tracked["nodes"][action._idx]
    return entry.get("namespace_stack", []), entry.get("explicit_namespace")


class _TrackedComposableNode:
    """A composable node plugin loaded into a container process.

    Does NOT add to the flat ``_state.tracked["nodes"]`` list — it is attached to the
    container's ``plugins`` list when the container is resolved in ``_walk_action``.
    """

    def __init__(self, *, package=None, plugin=None, name=None, **kwargs):
        _track_package(package)
        self._raw_package = package
        self._package = str(package) if package else ""
        self._raw_plugin = plugin
        self._plugin = str(plugin) if plugin else ""
        self._raw_name = name
        self._name = str(name) if name else ""
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        # Eager: track any ParameterFile paths
        for p in self._raw_parameters:
            if hasattr(p, "_param_file") and p._param_file:
                _track_param_file(p._param_file)

    def __repr__(self):
        return f"TrackedComposableNode(package={self._package!r}, plugin={self._plugin!r})"


class _TrackedComposableNodeContainer:
    """A composable node container process.

    Emits a ``kind='container'`` entry whose ``plugins`` list is populated during
    deferred resolution in ``_walk_action`` from the *composable_node_descriptions*.
    """

    def __init__(
        self,
        *,
        package=None,
        executable=None,
        name=None,
        composable_node_descriptions=None,
        **kwargs,
    ):
        _track_package(package)
        self._idx = _track_node(
            {
                "package": str(package) if package else "",
                "executable": str(executable) if executable else "",
                "name": str(name) if name else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "container",
                "plugins": [],
                "target": None,
            }
        )
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False
        # Eager: track packages from descriptions — pass raw object so
        # _track_package can filter out substitution objects.
        for desc in self._descs:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                _track_package(raw_pkg)


class _TrackedLoadComposableNodes:
    """Loads composable nodes into an existing container.

    Emits a ``kind='load_composable'`` entry with ``target`` set to the container
    name and ``plugins`` populated during deferred resolution in ``_walk_action``.
    """

    def __init__(self, *, composable_node_descriptions=None, target_container=None, **kwargs):
        # Eagerly resolve target_container when it's a plain string
        if target_container is None:
            target_str = ""
        elif isinstance(target_container, str):
            target_str = target_container
        elif isinstance(target_container, _TrackedComposableNodeContainer):
            # The container object itself was passed directly — retrieve its registered name.
            target_str = _state.tracked["nodes"][target_container._idx].get("name", "")
        elif hasattr(target_container, "perform"):
            try:
                result = target_container.perform(_StubLaunchContext())
                target_str = str(result) if result is not None else str(target_container)
            except Exception:
                target_str = str(target_container)
        else:
            target_str = str(target_container)
        self._raw_target = target_container
        self._idx = _track_node(
            {
                "package": "",
                "executable": "",
                "name": "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "load_composable",
                "plugins": [],
                "target": target_str,
            }
        )
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False
        # Eager: track packages from descriptions — pass raw object so
        # _track_package can filter out substitution objects.
        for desc in self._descs:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                _track_package(raw_pkg)


class _TrackedPushRosNamespace:
    """Tracks PushRosNamespace so _walk_action can update _state.namespace_stack."""

    def __init__(self, namespace=None, **kwargs):
        self._namespace = namespace  # string or substitution object


class _TrackedParameterFile:
    """Tracks ParameterFile references so they can be reported as param_file dependencies."""

    def __init__(self, param_file=None, *args, allow_substs=False, **kwargs):
        # param_file may be positional or keyword, and may be a string or substitution
        if param_file is None and args:
            param_file = args[0]
        self._param_file = None
        self._raw_param_file = param_file  # Keep raw for deferred resolution
        if param_file is not None:
            if isinstance(param_file, str):
                path = param_file  # Keep portable; Rust handles $(find-pkg-share ...) format
            elif hasattr(param_file, "perform"):
                try:
                    result = param_file.perform(_StubLaunchContext())
                    # Only use eagerly-resolved path if it's meaningful (not None)
                    path = str(result) if result is not None else None
                except Exception:
                    path = None  # Defer to _resolve_node_details
            else:
                path = str(param_file) if param_file is not None else None
            if path:
                self._param_file = path
                _track_param_file(path)


class _SetLaunchConfiguration:
    """Implements SetLaunchConfiguration: updates launch_configurations at walk time."""

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value


class _TimerAction:
    """Stores TimerAction child actions so the walker can recurse into them."""

    def __init__(self, *, period=None, actions=None, **kwargs):
        self._actions = list(actions or [])


class _TrackedFindPackageShare:
    """Tracks FindPackageShare; package may be a string or a list of substitutions."""

    def __init__(self, package):
        self._package_subs = package
        # Track statically when the name is a plain string
        if isinstance(package, str):
            _track_package(package)

    def _resolve_name(self, context=None):
        """Concatenate package name from string or list of substitution objects.

        Returns ``(name, is_fallback)`` where *is_fallback* is ``True`` when
        any part could not be resolved and the display name was used instead.
        """
        subs = self._package_subs
        if isinstance(subs, str):
            return subs, False
        if isinstance(subs, list):
            parts = []
            any_fallback = False
            for sub in subs:
                if context is not None and hasattr(sub, "perform"):
                    result = sub.perform(context)
                    if result is not None:
                        parts.append(str(result))
                    else:
                        parts.append(str(sub))
                        any_fallback = True
                else:
                    parts.append(str(sub))
                    if hasattr(sub, "perform"):
                        any_fallback = True
            return "".join(parts), any_fallback
        if hasattr(subs, "perform"):
            return str(subs), True
        return str(subs), False

    def _try_ament_resolve(self, pkg: str) -> str:
        """Resolve to a real path, return portable form on failure."""
        try:
            return _resolve_pkg_share(pkg)
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            if not _state.preview_mode:
                _error(f"$(find-pkg-share {pkg}): {e}")
            return f"$(find-pkg-share {pkg})"

    def perform(self, context):
        pkg, is_fallback = self._resolve_name(context)
        if not is_fallback:
            _track_package(pkg)
        if not _state.preview_mode:
            return self._try_ament_resolve(pkg)
        return f"$(find-pkg-share {pkg})"

    def __str__(self):
        pkg, is_fallback = self._resolve_name(None)
        if not _state.preview_mode:
            return self._try_ament_resolve(pkg)
        return f"$(find-pkg-share {pkg})"


class _TrackedPathJoinSubstitution:
    def __init__(self, substitutions):
        self._subs = substitutions
        # Track packages from nested FindPackageShare
        for sub in substitutions:
            if isinstance(sub, _TrackedFindPackageShare):
                pkg, is_fallback = sub._resolve_name()
                if not is_fallback:
                    _track_package(pkg)

    def perform(self, context):
        parts = []
        for sub in self._subs:
            if hasattr(sub, "perform"):
                result = sub.perform(context)
                parts.append(str(result) if result is not None else str(sub))
            else:
                parts.append(str(sub))
        return str(Path(*parts))


def _resolve_include_args(path, launch_arguments, context, dep_idx=-1):
    """Capture launch_arguments for an include site.

    When dep_idx >= 0, stores args directly into the include_deps entry
    (allowing distinct args per include site).  Falls back to the shared
    include_args dict for backward compatibility.

    Called both at construction time (with a stub context) and deferred during
    _walk_action (with the live context, which can resolve LaunchConfiguration
    and PathJoinSubstitution values correctly).  A second call for the same
    entry is skipped — the first resolved value wins.
    """
    if not launch_arguments or not path:
        return
    path = str(path)
    # Check if this entry already has args resolved.
    if dep_idx >= 0 and _state.tracked["include_deps"][dep_idx].get("include_args"):
        return
    if dep_idx < 0 and path in _state.tracked["include_args"]:
        return
    try:
        captured = {}
        for k, v in launch_arguments:
            k_str = str(k)
            if isinstance(v, list):
                parts = []
                for sub in v:
                    if hasattr(sub, "perform"):
                        try:
                            result = sub.perform(context)
                            parts.append(str(result) if result is not None else str(sub))
                        except _PackageNotFetchedError:
                            raise
                        except Exception:
                            parts.append(str(sub))
                    else:
                        parts.append(str(sub))
                v_str = "".join(parts)
            elif hasattr(v, "perform"):
                try:
                    result = v.perform(context)
                    v_str = str(result) if result is not None else str(v)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    v_str = str(v)
            else:
                v_str = str(v)
            captured[k_str] = v_str
        if captured:
            if dep_idx >= 0:
                _state.tracked["include_deps"][dep_idx]["include_args"] = captured
            else:
                _state.tracked["include_args"][path] = captured
    except _PackageNotFetchedError:
        raise
    except Exception as e:
        _warn(f"failed to resolve include args for '{path}': {e}")


class _TrackedIncludeLaunchDescription:
    def __init__(self, launch_description_source, launch_arguments=None, **kwargs):
        self._source = launch_description_source
        # Save raw args for deferred resolution in _walk_action (needed when the source
        # is a substitution like PathJoinSubstitution that can only be resolved with a
        # live LaunchContext — typically when constructed inside an OpaqueFunction).
        self._raw_launch_arguments = launch_arguments
        # Extract path from the source
        path = None
        if hasattr(launch_description_source, "_location"):
            path = launch_description_source._location
        elif hasattr(launch_description_source, "location"):
            try:
                path = launch_description_source.location
            except Exception:
                pass
        self._path = path
        self._dep_idx = -1
        if path:
            self._dep_idx = _track_include(path)

        # Capture launch_arguments so the orchestrator can forward them when it recursively
        # invokes py_resolver for the included file.  Values may be strings, lists of
        # substitution objects (common when constructed inside OpaqueFunction where pkg paths
        # are already resolved), or substitution objects; resolve eagerly where possible.
        if launch_arguments and path:
            _resolve_include_args(path, launch_arguments, _StubLaunchContext(), self._dep_idx)


class _DeferredDefault:
    """Wraps an unresolved DeclareLaunchArgument default_value.

    Stored in ``_launch_configurations`` instead of a resolved string.
    Resolution is deferred until the value is actually read via
    ``_LaunchConfiguration.perform()``.  This avoids eagerly calling
    ``FindPackageShare.perform()`` for packages that may not be installed
    when the arg is never actually used (e.g. gated by a false condition).
    """

    def __init__(self, default_value):
        self.default_value = default_value

    def resolve(self, context):
        """Resolve the deferred substitutions to a string."""
        dv = self.default_value
        if hasattr(dv, "perform"):
            result = dv.perform(context)
            return str(result) if result is not None else str(dv)
        if isinstance(dv, list):
            parts = []
            for sub in dv:
                if hasattr(sub, "perform"):
                    result = sub.perform(context)
                    parts.append(str(result) if result is not None else str(sub))
                else:
                    parts.append(str(sub))
            return "".join(parts)
        return str(dv)


class _LaunchConfiguration:
    """Substitution that resolves to a launch configuration value at runtime."""

    def __init__(self, variable_name, default=None, **kwargs):
        self._name = variable_name
        self._default = default

    def perform(self, context):
        if context and hasattr(context, "_launch_configurations"):
            if self._name in context._launch_configurations:
                value = context._launch_configurations[self._name]
                # Resolve deferred defaults on first read.
                if isinstance(value, _DeferredDefault):
                    resolved = value.resolve(context)
                    context._launch_configurations[self._name] = resolved
                    return resolved
                return value
            if self._default is not None:
                return str(self._default)
        return None

    def __str__(self):
        return self._name


class _DeclaredArg:
    """Stub for DeclareLaunchArgument: captures name, default_value, and condition."""

    def __init__(self, name=None, *positional, default_value=None, condition=None, **kwargs):
        # ROS 2 signature: DeclareLaunchArgument(name, *, default_value=None, ...)
        # `name` may be passed positionally or as keyword.
        self.name = str(name) if name is not None else (str(positional[0]) if positional else None)
        self.default_value = default_value
        self.condition = condition


def _apply_declared_arg(arg: "_DeclaredArg", context) -> None:
    """Resolve a DeclareLaunchArgument default and apply it to the launch context.

    Two effects:
      1. Sets context._launch_configurations[name] if the arg is not already provided
         (CLI/parent args take precedence), making the default available to OpaqueFunction.
      2. Records the resolved default in _state.tracked["declared_args"] so the Rust orchestrator
         can forward it to child launch files according to the apply_arg_defaults flag.

    Processing order mirrors the XML resolver: each declaration is resolved immediately
    so that a later DeclareLaunchArgument can reference an earlier one via
    LaunchConfiguration("prev_arg").
    """
    if not arg.name:
        return

    # Evaluate condition if present — skip if the condition evaluates to False.
    if arg.condition is not None and hasattr(arg.condition, "evaluate"):
        try:
            if not arg.condition.evaluate(context):
                return
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(
                f"condition on DeclareLaunchArgument '{arg.name}' failed to evaluate: {e}; "
                f"assuming condition is satisfied"
            )

    if arg.default_value is None:
        # Required arg with no default — record declaration but nothing to resolve.
        already_seen = arg.name in _state.declared_arg_names
        if not already_seen:
            _state.declared_arg_names.add(arg.name)
        _record_declared_arg(arg.name, "", flat=not already_seen)
        return

    # Lazy evaluation: if the arg is already set by the caller, skip resolving the
    # default (which may trigger side effects like FindPackageShare for packages that
    # aren't installed).  Record the unresolved default string for --show-args.
    already_set = context is not None and arg.name in context._launch_configurations
    if already_set:
        # Record unresolved default for --show-args metadata.
        # Use _portable_display() to avoid triggering resolution side effects.
        dv = arg.default_value
        if isinstance(dv, list):
            raw = "".join(_portable_display(s) for s in dv)
        else:
            raw = _portable_display(dv)
        already_seen = arg.name in _state.declared_arg_names
        if not already_seen:
            _state.declared_arg_names.add(arg.name)
        _record_declared_arg(arg.name, raw, flat=not already_seen)
        return

    # In strict mode (apply_arg_defaults=False), skip applying the default.
    # LaunchConfiguration.perform() will return None, surfacing as "undefined variable".
    if not _state.apply_arg_defaults:
        _record_declared_arg(arg.name, "", flat=arg.name not in _state.declared_arg_names)
        if arg.name not in _state.declared_arg_names:
            _state.declared_arg_names.add(arg.name)
        return

    # Store the default as a _DeferredDefault — resolution is deferred until the
    # value is actually read via LaunchConfiguration.perform().  This avoids
    # eagerly resolving FindPackageShare for packages that may not be installed
    # (e.g. CUDA packages) when the arg is gated by a false condition and never
    # actually read.
    #
    # For --show-args metadata, record the portable display form (no side effects).
    dv = arg.default_value
    if isinstance(dv, list):
        display = "".join(_portable_display(s) for s in dv)
    else:
        display = _portable_display(dv)

    # Record for the Rust orchestrator (first declaration wins in flat list; per-file always).
    already_seen = arg.name in _state.declared_arg_names
    if not already_seen:
        _state.declared_arg_names.add(arg.name)
    _record_declared_arg(arg.name, display, flat=not already_seen)

    # Apply deferred default to the launch context — resolved on first read.
    if context is not None:
        context._launch_configurations[arg.name] = _DeferredDefault(dv)


class _TrackedOpaqueFunction:
    """Stores an OpaqueFunction's callable so the walker can invoke it."""

    def __init__(self, *, function=None, **kwargs):
        self.function = function


class _TrackedGroupAction:
    """Stores GroupAction's child actions so the walker can recurse into them."""

    def __init__(self, actions=None, **kwargs):
        self._actions = list(actions or [])
        self._scoped = kwargs.get("scoped", True)
        self._condition = kwargs.get("condition")


class _TrackedSetEnvironmentVariable:
    """Tracks SetEnvironmentVariable: mutates _state.env in _walk_action."""

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value
        self._condition = kwargs.get("condition")


class _TrackedUnsetEnvironmentVariable:
    """Tracks UnsetEnvironmentVariable: removes from _state.env in _walk_action."""

    def __init__(self, name=None, **kwargs):
        self._name = name
        self._condition = kwargs.get("condition")


class _TrackedSetParameter:
    """Mirrors launch_ros SetParameter: accumulates (name, value) into context['global_params'].

    In the real ROS 2 launch system, SetParameter.execute() appends (name, ParameterValue)
    tuples to context.launch_configurations['global_params'].  Child launch files (e.g.
    ground_segmentation.launch.py) read this list to access vehicle dimensions and similar
    cross-file global state.  We reproduce this here so the walker can populate the stub
    context and the orchestrator can persist the params across isolated py_resolver calls.
    """

    def __init__(self, name=None, value=None, **kwargs):
        # Keep raw for deferred resolution in _walk_action (value may be a substitution).
        # No node entry — SetParameter is a side-effect action that populates _state.global_params,
        # which are then absorbed into each leaf node's parameters.
        self._name = name
        self._value = value


class _TrackedExecutable:
    """Tracks an ExecuteProcess so the walker can render it as <executable>."""

    def __init__(self, *, cmd=None, name=None, shell=False, **kwargs):
        # `cmd` may be a list of substitution-bearing strings or a plain string.
        if isinstance(cmd, list):
            self._cmd = cmd
        elif cmd is not None:
            self._cmd = [cmd]
        else:
            self._cmd = []
        self._name = name
        self._shell = bool(shell)
        self._idx = _track_node(
            {
                "package": "",
                "executable": "",
                "name": str(name) if name is not None and not hasattr(name, "perform") else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "executable",
                "plugins": [],
                "target": None,
                "cmd": "",
                "shell": self._shell,
            }
        )


# ─── LaunchContext stub ────────────────────────────────────────────────────────


class _StubLaunchContext:
    """Minimal LaunchContext: holds launch_configurations for substitution resolution."""

    def __init__(self):
        self._launch_configurations = {}

    @property
    def launch_configurations(self):
        return self._launch_configurations

    @launch_configurations.setter
    def launch_configurations(self, value):
        self._launch_configurations = value

    def perform_substitution(self, sub):
        if hasattr(sub, "perform"):
            return sub.perform(self)
        return str(sub)


# ─── Param-file stubs for OpaqueFunction resilience ─────────────────────────
#
# OpaqueFunction bodies in Autoware typically call `open(param_file)` and
# `yaml.safe_load(f)` to read ROS 2 parameter YAML files before constructing
# their composable nodes.  In preview mode these paths are resolved to
# workspace-relative strings which may not exist (e.g. naming mismatches
# between the autoware_launch config and the package's own config directory).
#
# To recover from this edge case we temporarily patch `builtins.open` and
# `yaml.safe_load` during OpaqueFunction execution:
#   - open(): if the requested file is not found, emit a warning and return a
#     StringIO stub containing a minimal valid ROS 2 parameter YAML.
#   - yaml.safe_load(): wraps any `ros__parameters` dict in `_DefaultParamDict`
#     so that missing keys return `False` instead of raising `KeyError`.
#     Boolean-flag guards such as `if params["downsample_input_pointcloud"]:`
#     then evaluate to False, skipping optional branches while still letting
#     the function reach and register its core composable nodes.
#
# The patches are installed/removed atomically around `fn(context)` via
# `_call_opaque_with_stubs`.

import builtins as _builtins
import io as _io
import pathlib as _pathlib

_STUB_ROS_PARAM_YAML = "/**:\n  ros__parameters: {}\n"


class _DefaultParamDict(dict):
    """Dict wrapper that returns a falsy, dict-like sentinel for missing keys.

    Used as a stand-in for a ROS 2 ``ros__parameters`` dict when the param
    file could not be read.

    Returning an empty ``_DefaultParamDict()`` (rather than ``False``) satisfies
    both common usage patterns:

    1. **Boolean guards** — ``if params["flag"]:`` evaluates to ``False``
       because an empty dict is falsy, so optional feature branches are skipped.
    2. **Dict operations** — code that calls ``.update()``, ``len()``, or
       iterates over a nested param block (e.g. ``fusion_config``) succeeds
       without raising ``AttributeError: 'bool' object has no attribute 'update'``.
    """

    def __missing__(self, key):
        return _DefaultParamDict()


def _call_opaque_with_stubs(fn, context):
    """Call ``fn(context)`` with file-not-found stubs active.

    In **preview mode**, patches ``builtins.open``, ``yaml.safe_load``, and
    ``os.path`` predicates for the duration of the call so that portable
    ``$(find-pkg-share pkg)/...`` paths are intercepted and resolved to actual
    filesystem paths via the ``_state.package_shares`` map.  If the target package
    exists in the lockfile but hasn't been fully fetched yet (no
    ``package.xml``), ``_PackageNotFetchedError`` is raised immediately so the
    Rust orchestrator can fetch the package and retry.

    In **non-preview mode** (post-build), all packages are installed and
    ``FindPackageShare`` returns real AMENT paths, so ``open()`` and
    ``os.path.*`` work natively.  No shimming is performed.
    """
    # Non-preview: packages are installed, paths are real — no shimming needed.
    if not _state.preview_mode:
        return fn(context)

    import yaml as _yaml

    _orig_open = _builtins.open
    _orig_safe_load = _yaml.safe_load
    _orig_path_exists = os.path.exists
    _orig_path_isfile = os.path.isfile
    _orig_path_isdir = os.path.isdir
    _orig_pathlib_open = _pathlib.Path.open

    def _resolve_portable(path_str: str):
        """Resolve a portable path to an actual filesystem path.

        Returns the resolved path string, or ``None`` if the input is not a
        portable path.  Fetches the package inline if needed via _ensure_fetched().
        Raises ``_PackageNotFetchedError`` only if fetching fails.
        """
        parsed = _parse_portable_path(path_str)
        if parsed is None:
            return None
        pkg, rest = parsed
        if pkg in _state.package_shares:
            pkg_dir = _state.package_shares[pkg]
            if not _orig_path_isfile(os.path.join(pkg_dir, "package.xml")):
                if _ensure_fetched(pkg):
                    pkg_dir = _state.package_shares[pkg]
                else:
                    raise _PackageNotFetchedError(pkg)
            return os.path.join(pkg_dir, rest) if rest else pkg_dir
        # Try fetching if it's a lockfile package.
        if pkg in _state.lockfile_data and _ensure_fetched(pkg):
            pkg_dir = _state.package_shares[pkg]
            return os.path.join(pkg_dir, rest) if rest else pkg_dir
        # Try AMENT_PREFIX_PATH for packages not in the lockfile.
        if _real_get_package_share_directory is not None:
            try:
                share = _real_get_package_share_directory(pkg)
                return os.path.join(share, rest) if rest else share
            except _PackageNotFetchedError:
                raise
            except Exception:
                pass
        # Package not found anywhere that we know about — can't resolve.
        return None

    def _stub_open(path, mode="r", *args, **kwargs):
        path_str = str(path)
        # Handle portable paths.
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction opened a portable path without --apply-opaque-file-access "
                    f"(stub returned): {path}"
                )
                return _io.StringIO(_STUB_ROS_PARAM_YAML)
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                try:
                    return _orig_open(actual, mode, *args, **kwargs)
                except (FileNotFoundError, OSError):
                    _error(
                        f"param file not found: '{actual}' (resolved from '{path}') — stub defaults used"
                    )
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            _error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)
        # Regular (non-portable) path.
        try:
            return _orig_open(path, mode, *args, **kwargs)
        except (FileNotFoundError, OSError):
            # If the missing file is inside a lockfile package that hasn't been fully
            # fetched yet (no package.xml), try to fetch it inline.
            for pkg_name, pkg_dir in list(_state.package_shares.items()):
                pkg_dir_norm = pkg_dir.rstrip("/")
                if path_str.startswith(pkg_dir_norm + "/") or path_str.startswith(
                    pkg_dir_norm + os.sep
                ):
                    if not _orig_path_isfile(os.path.join(pkg_dir_norm, "package.xml")):
                        if _ensure_fetched(pkg_name):
                            # Retry open after fetching.
                            try:
                                return _orig_open(path, mode, *args, **kwargs)
                            except (FileNotFoundError, OSError):
                                pass  # File still missing after fetch → fall through to error
                        else:
                            raise _PackageNotFetchedError(pkg_name) from None
                    break  # package is fully fetched; file genuinely missing → error
            _error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)

    def _stub_path_exists(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.exists on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                return _orig_path_exists(actual)
            return False
        return _orig_path_exists(path)

    def _stub_path_isfile(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.isfile on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                return _orig_path_isfile(actual)
            return False
        return _orig_path_isfile(path)

    def _stub_path_isdir(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.isdir on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                return _orig_path_isdir(actual)
            return False
        return _orig_path_isdir(path)

    def _stub_pathlib_open(
        path_self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        """Intercept Path.open() so that Path(...).read_text() also handles portable paths.

        pathlib.Path.read_text() calls self.open() internally, bypassing builtins.open.
        This stub redirects the call to the resolved actual path when the Path object
        holds a portable $(find-pkg-share ...) string.
        """
        path_str = str(path_self)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called Path.open on a portable path without "
                    f"--apply-opaque-file-access (stub returned): {path_str}"
                )
                return _io.StringIO(_STUB_ROS_PARAM_YAML)
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                try:
                    return _orig_pathlib_open(
                        _pathlib.Path(actual), mode, buffering, encoding, errors, newline
                    )
                except (FileNotFoundError, OSError):
                    _error(
                        f"param file not found: '{actual}' (resolved from '{path_str}') — stub defaults used"
                    )
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            _error(f"param file not found: '{path_str}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)
        return _orig_pathlib_open(path_self, mode, buffering, encoding, errors, newline)

    def _patched_safe_load(stream):
        result = _orig_safe_load(stream)
        # Wrap ros__parameters dicts in _DefaultParamDict so that missing
        # keys return False rather than raising KeyError.
        if isinstance(result, dict):
            for _ns_key, ns_val in result.items():
                if isinstance(ns_val, dict) and "ros__parameters" in ns_val:
                    rp = ns_val["ros__parameters"]
                    if isinstance(rp, dict):
                        ns_val["ros__parameters"] = _DefaultParamDict(rp)
        return result

    _builtins.open = _stub_open
    _yaml.safe_load = _patched_safe_load
    os.path.exists = _stub_path_exists
    os.path.isfile = _stub_path_isfile
    os.path.isdir = _stub_path_isdir
    _pathlib.Path.open = _stub_pathlib_open
    try:
        return fn(context)
    finally:
        _builtins.open = _orig_open
        _yaml.safe_load = _orig_safe_load
        os.path.exists = _orig_path_exists
        os.path.isfile = _orig_path_isfile
        os.path.isdir = _orig_path_isdir
        _pathlib.Path.open = _orig_pathlib_open


# ─── LaunchContext factory ────────────────────────────────────────────────────


def _make_launch_context(args_dict):
    """Create a LaunchContext pre-populated with provided args."""
    try:
        from launch import LaunchContext

        ctx = LaunchContext()
        ctx._launch_configurations = dict(args_dict)
        return ctx
    except Exception:
        ctx = _StubLaunchContext()
        ctx._launch_configurations = dict(args_dict)
        return ctx


# ─── Node detail resolution helpers ──────────────────────────────────────────


def _env_overrides():
    """Return a copy of the current env overrides for per-node output."""
    return dict(_state.env)


def _resolve_node_details(node, context):
    """Fill in deferred details (package, executable, name, namespace, params, remaps, env).

    Works for both ``_TrackedNode`` / ``_TrackedLifecycleNode`` and
    ``_TrackedComposableNodeContainer`` — both expose the same raw fields.
    """
    entry = _state.tracked["nodes"][node._idx]

    # Package / executable / name: resolve substitutions (e.g. LaunchConfiguration)
    # that could not be resolved at construction time.
    for field_name in ("package", "executable", "name"):
        raw = getattr(node, f"_raw_{field_name}", None)
        if _is_substitution(raw):
            resolved, is_fallback = _resolve_substitution_ex(raw, context)
            if resolved is not None:
                entry[field_name] = resolved
                if field_name == "package" and not is_fallback:
                    _track_package(resolved)

    # Namespace: emit raw inputs — Rust computes effective_namespace from these
    ns = (
        _resolve_substitution(node._raw_namespace, context)
        if node._raw_namespace is not None
        else None
    )
    entry["namespace_stack"] = list(_state.namespace_stack)
    entry["explicit_namespace"] = ns

    # Parameters and param files
    params = {}
    pf_list: list[dict] = []
    seen_pf: set[str] = set()
    for p in node._raw_parameters:
        if hasattr(p, "_param_file"):
            path = p._param_file
            # Deferred resolution: try again with live context if not resolved eagerly
            if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                raw = p._raw_param_file
                if hasattr(raw, "perform"):
                    try:
                        result = raw.perform(context)
                        if result is not None:
                            path = str(result)
                    except Exception:
                        pass
                elif not isinstance(raw, str):
                    path = str(raw)
            if path:
                path = str(path)
                if path not in seen_pf:
                    seen_pf.add(path)
                    pf_entry: dict = {"path": path}
                    if _state.inline_params:
                        expanded = _read_and_expand_param_file(path)
                        if expanded is not None:
                            pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
        elif isinstance(p, dict):
            for k, v in p.items():
                resolved_v = _resolve_substitution(v, context)
                params[str(k)] = resolved_v if resolved_v is not None else ""
    # Merge global params from the launch context (global first, node-local overrides).
    # In ROS 2, Node.execute() reads global_params from context._launch_configurations.
    # ComposableNodeContainer inherits from Node, so it also gets global params.
    ctx_global_params = context._launch_configurations.get("global_params", [])
    merged_params = {k: str(v) for k, v in ctx_global_params}
    merged_params.update(params)
    entry["parameters"] = merged_params
    entry["param_files"] = list(_state.global_param_files) + pf_list

    # Remappings: list of [src, dst] pairs — merge global remaps first
    remaps = list(_state.global_remaps)
    for r in node._raw_remappings:
        if isinstance(r, (tuple, list)) and len(r) == 2:
            src = _resolve_substitution(r[0], context)
            dst = _resolve_substitution(r[1], context)
            remaps.append([src or str(r[0]), dst or str(r[1])])
    entry["remappings"] = remaps

    # Env vars: start with inherited env diff, then node-local overrides
    env = _env_overrides()
    raw_env = node._raw_env
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            env[_resolve_substitution(k, context) or str(k)] = (
                _resolve_substitution(v, context) or ""
            )
    elif isinstance(raw_env, (list, tuple)):
        for item in raw_env:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                k_str = _resolve_substitution(item[0], context) or str(item[0])
                v_str = _resolve_substitution(item[1], context) or ""
                env[k_str] = v_str
    entry["env"] = env

    # output / arguments / respawn / respawn_delay
    for attr, key in (
        ("_raw_output", "output"),
        ("_raw_arguments", "args"),
        ("_raw_respawn", "respawn"),
        ("_raw_respawn_delay", "respawn_delay"),
    ):
        raw = getattr(node, attr, None)
        if raw is not None:
            resolved = _resolve_substitution(raw, context)
            if resolved is not None:
                entry[key] = resolved
            elif isinstance(raw, str):
                entry[key] = raw
            else:
                entry[key] = str(raw)


def _resolve_composable_plugins(descs, context):
    """Convert ``_TrackedComposableNode`` descriptions to serialisable plugin dicts."""
    plugins = []
    for desc in descs:
        if not isinstance(desc, _TrackedComposableNode):
            # Fallback for real/unknown description objects
            pkg = getattr(desc, "package", None) or getattr(desc, "_package", None)
            plugin = getattr(desc, "plugin", None) or getattr(desc, "_plugin", None)
            if pkg or plugin:
                plugins.append(
                    {
                        "package": str(pkg) if pkg else "",
                        "plugin": str(plugin) if plugin else "",
                        "name": None,
                        "parameters": {},
                        "remappings": [],
                    }
                )
            continue
        params = {}
        pf_list: list[dict] = []
        seen_pf: set[str] = set()
        for p in desc._raw_parameters:
            if hasattr(p, "_param_file"):
                path = p._param_file
                # Deferred resolution: try again with live context if not resolved eagerly
                if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                    raw = p._raw_param_file
                    if hasattr(raw, "perform"):
                        try:
                            result = raw.perform(context)
                            if result is not None:
                                path = str(result)
                        except Exception:
                            pass
                    elif not isinstance(raw, str):
                        path = str(raw)
                if path:
                    path = str(path)
                    if path not in seen_pf:
                        seen_pf.add(path)
                        pf_entry: dict = {"path": path}
                        if _state.inline_params:
                            expanded = _read_and_expand_param_file(path)
                            if expanded is not None:
                                pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = _resolve_substitution(v, context)
                    params[str(k)] = resolved_v if resolved_v is not None else ""
        remaps = []
        for r in desc._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = _resolve_substitution(r[0], context)
                dst = _resolve_substitution(r[1], context)
                remaps.append(
                    [
                        src if src is not None else str(r[0]),
                        dst if dst is not None else str(r[1]),
                    ]
                )
        # Resolve package/plugin/name substitutions with the live context
        pkg = desc._package
        if _is_substitution(desc._raw_package):
            resolved_pkg, is_fallback = _resolve_substitution_ex(desc._raw_package, context)
            if resolved_pkg is not None:
                pkg = resolved_pkg
                if not is_fallback:
                    _track_package(resolved_pkg)
        plg = desc._plugin
        if _is_substitution(desc._raw_plugin):
            resolved_plg = _resolve_substitution(desc._raw_plugin, context)
            if resolved_plg is not None:
                plg = resolved_plg
        nm = desc._name
        if _is_substitution(desc._raw_name):
            resolved_nm = _resolve_substitution(desc._raw_name, context)
            if resolved_nm is not None:
                nm = resolved_nm
        plugins.append(
            {
                "package": pkg,
                "plugin": plg,
                "name": nm or None,
                "parameters": params,
                "remappings": remaps,
                "param_files": pf_list,
            }
        )
    return plugins


# ─── Inline Python include resolution ─────────────────────────────────────────


def _inline_resolve_python_launch(launch_file, parent_context, child_args, depth):
    """Load a Python launch file and walk its actions in the parent context.

    This mirrors real ROS 2 behavior where ``IncludeLaunchDescription``
    synchronously executes the child, so ``SetLaunchConfiguration`` calls in
    the child mutate the shared ``LaunchContext``.  The orchestrator still
    handles the recursive node/include dependency resolution separately.
    """
    if depth > 20:
        _warn(f"Max inline include depth for {launch_file}")
        return

    # Resolve portable paths — $(find-pkg-share pkg)/rest → real filesystem path.
    real_path = launch_file
    parsed = _parse_portable_path(launch_file)
    if parsed:
        pkg, rest = parsed
        try:
            pkg_share = _resolve_pkg_share(pkg)
        except _PackageNotFetchedError:
            raise
        except Exception:
            return  # Package not available — orchestrator will resolve later
        real_path = os.path.join(pkg_share, rest)

    if not os.path.isfile(real_path):
        return  # File not on disk — orchestrator will fetch and resolve later

    try:
        spec = importlib.util.spec_from_file_location(f"_inline_launch_{depth}", real_path)
        if spec is None or spec.loader is None:
            _warn(f"cannot load included launch file: {real_path}")
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except _PackageNotFetchedError:
        raise
    except Exception as e:
        _warn(f"failed to load included launch file {real_path}: {e}")
        return

    if not hasattr(mod, "generate_launch_description"):
        return

    # Save scoping state that must be restored after inline execution.
    # Nodes, includes, packages, params, etc. are KEPT — the Python resolver
    # now handles all includes inline and the orchestrator does NOT re-resolve them.
    saved_declared_arg_names = set(_state.declared_arg_names)
    saved_namespace_depth = len(_state.namespace_stack)
    saved_env = dict(_state.env)

    # Snapshot parent context BEFORE generate_launch_description() so we can
    # fully restore it after the inline walk.  Only deliberate side-effects
    # (SetLaunchConfiguration) survive.
    saved_configs = dict(parent_context._launch_configurations)

    # Most _state.tracked mutations (from constructors in generate_launch_description
    # AND from _walk_actions) must be rolled back on any exit path, including
    # exceptions from generate_launch_description() itself.
    #
    # Intentionally preserved side-effects (NOT rolled back):
    #   - set_launch_configurations: the whole purpose of inline includes
    #   - warnings / errors: diagnostic messages should propagate to the user
    # Everything else in _state.tracked is rolled back in the finally block below.
    try:
        try:
            ld = mod.generate_launch_description()
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"generate_launch_description() failed in {launch_file}: {e}")
            return

        entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

        # Apply child launch arguments: explicit include args override parent context.
        # In strict mode (global_arg_cascade=False), strip parent context so the
        # child only sees explicitly forwarded args + its own DeclareLaunchArgument.
        if not _state.global_arg_cascade:
            parent_context._launch_configurations.clear()
        for k, v in child_args.items():
            parent_context._launch_configurations[k] = v

        # Pass 1: apply DeclareLaunchArgument defaults (child-only args).
        for entity in entities:
            if isinstance(entity, _DeclaredArg):
                _apply_declared_arg(entity, parent_context)

        # Pass 2: walk actions — SetLaunchConfiguration, OpaqueFunction, etc.
        # all mutate parent_context directly, which is the desired effect.
        # Only context mutations survive; tracked state is rolled back.
        _walk_actions(entities, parent_context, depth)
    except _PackageNotFetchedError:
        raise
    finally:
        # Restore scoping state only — nodes, includes, packages, params, etc.
        # are intentionally kept since the orchestrator no longer re-resolves them.
        _state.declared_arg_names.clear()
        _state.declared_arg_names.update(saved_declared_arg_names)
        del _state.namespace_stack[saved_namespace_depth:]
        _state.env.clear()
        _state.env.update(saved_env)

    # Restore child-only args that were not SetLaunchConfiguration'd —
    # child DeclareLaunchArgument defaults should NOT leak into the parent
    # scope, only SetLaunchConfiguration is a deliberate side-effect.
    # Keep keys that were either already in the parent or were set via
    # SetLaunchConfiguration.  Also preserve "global_params" — this is the
    # accumulation list for SetParameter, not a launch argument.
    set_configs = set(_state.tracked["set_launch_configurations"].keys())
    for k in list(parent_context._launch_configurations):
        if k not in saved_configs and k not in set_configs and k != "global_params":
            del parent_context._launch_configurations[k]


# ─── Action walker ────────────────────────────────────────────────────────────


def _walk_actions(actions, context, depth=0):
    if depth > 20:
        _warn("Max include depth reached while walking Python launch description")
        return
    if actions is None:
        return
    for action in actions:
        if action is None:
            continue
        try:
            _walk_action(action, context, depth)
        except _PackageNotFetchedError:
            raise  # Let it propagate to the OpaqueFunction or top-level handler
        except Exception as e:
            _error(f"Error walking action {type(action).__name__}: {e}")


def _walk_action(action, context, depth):
    cls_name = type(action).__name__

    # DeclareLaunchArgument: apply default to context so subsequent actions can use it.
    # This handles nested declarations (inside GroupAction etc.); top-level ones are
    # already processed in the two-pass logic in main().
    if isinstance(action, _DeclaredArg):
        _apply_declared_arg(action, context)
        return

    # Our tracked nodes — resolve details deferred with the live context
    if isinstance(action, (_TrackedNode, _TrackedLifecycleNode)):
        if context is not None and not action._detailed:
            action._detailed = True
            _resolve_node_details(action, context)
        return

    if isinstance(action, _TrackedComposableNodeContainer):
        if context is not None and not action._detailed:
            action._detailed = True
            _resolve_node_details(action, context)
            _state.tracked["nodes"][action._idx]["plugins"] = _resolve_composable_plugins(
                action._descs, context
            )
        return

    if isinstance(action, _TrackedLoadComposableNodes):
        if context is not None and not action._detailed:
            action._detailed = True
            entry = _state.tracked["nodes"][action._idx]
            if action._raw_target is not None:
                if isinstance(action._raw_target, _TrackedComposableNodeContainer):
                    # Container object: use its already-resolved name (updated by _resolve_node_details).
                    target = _state.tracked["nodes"][action._raw_target._idx].get(
                        "name"
                    ) or entry.get("target", "")
                else:
                    target = _resolve_substitution(action._raw_target, context)
                if target:
                    entry["target"] = target
            entry["plugins"] = _resolve_composable_plugins(action._descs, context)
        return

    if isinstance(action, _TrackedPushRosNamespace):
        if action._namespace is not None and context is not None:
            ns = _resolve_substitution(action._namespace, context)
            if ns:
                _state.namespace_stack.append(ns)
        return

    if isinstance(action, _TrackedIncludeLaunchDescription):
        # Deferred resolution: when the source is a substitution (e.g. PathJoinSubstitution),
        # the path cannot be extracted at construction time because no LaunchContext is
        # available.  Try again here with the live context provided by _walk_action.
        if action._path is None and context is not None:
            src = action._source
            path = None
            if hasattr(src, "perform"):
                try:
                    path = src.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception as e:
                    _warn(f"failed to resolve IncludeLaunchDescription source: {e}")
            if path:
                action._path = path
                dep_idx = _track_include(path)
                _resolve_include_args(path, action._raw_launch_arguments, context, dep_idx)

        # Inline-execute Python includes within the parent context so that
        # SetLaunchConfiguration side-effects propagate to sibling actions,
        # just like the real ROS 2 launch system processes includes synchronously.
        if action._path and action._path.endswith(".py") and context is not None:
            child_args = {}
            if action._raw_launch_arguments:
                for k, v in action._raw_launch_arguments:
                    k_str = str(k)
                    resolved = _resolve_substitution(v, context)
                    v_str = resolved if resolved is not None else str(v)
                    child_args[k_str] = v_str
            # Push include chain for source tracking.
            inc_dep = _extract_pkg_and_share_path(action._path)
            if inc_dep:
                _state.include_chain.append(list(inc_dep))
            else:
                _state.include_chain.append(["", action._path])
            try:
                _inline_resolve_python_launch(action._path, context, child_args, depth + 1)
            finally:
                _state.include_chain.pop()

        return

    # SetParameter: append (name, value) to context['global_params'], mirroring the real
    # launch_ros SetParameter.execute() which does:
    #   global_param_list = context.launch_configurations.get('global_params', [])
    #   global_param_list.extend(eval_param_dict.items())
    #   context.launch_configurations['global_params'] = global_param_list
    if isinstance(action, _TrackedSetParameter):
        name = action._name
        value = action._value
        # Resolve name substitution if needed
        if hasattr(name, "perform") and context is not None:
            try:
                name = name.perform(context)
            except _PackageNotFetchedError:
                raise
            except Exception:
                name = str(name)
        else:
            name = str(name) if name is not None else ""
        if name and context is not None:
            # Resolve value substitution if needed
            if hasattr(value, "perform"):
                try:
                    value = value.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    pass
            # Coerce string representations of numerics to native Python types so that
            # arithmetic in consumer files (e.g. gp["front_overhang"] + gp["wheel_base"])
            # works without explicit casts.
            if isinstance(value, str):
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        pass
            gp_list = context._launch_configurations.setdefault("global_params", [])
            gp_list.append((name, value))
            _state.tracked["global_params"].append([name, value])
            _state.global_params.append((name, value))
        return

    # ExecuteProcess: resolve cmd parts and name, record as executable.
    if isinstance(action, _TrackedExecutable):
        parts = []
        for part in action._cmd:
            raw_part = part
            if hasattr(part, "perform") and context is not None:
                try:
                    result = part.perform(context)
                    part = result if result is not None else raw_part
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    part = str(raw_part)
            parts.append(str(part))
        cmd_str = " ".join(parts)
        name = action._name
        if hasattr(name, "perform") and context is not None:
            try:
                name = name.perform(context)
            except _PackageNotFetchedError:
                raise
            except Exception:
                name = str(name) if name is not None else ""
        name_str = str(name) if name is not None else ""
        _state.tracked["nodes"][action._idx]["cmd"] = cmd_str
        _state.tracked["nodes"][action._idx]["name"] = name_str
        _state.tracked["nodes"][action._idx]["shell"] = action._shell
        return

    # SetLaunchConfiguration: update launch_configurations at walk time.
    if isinstance(action, _SetLaunchConfiguration):
        name = action._name
        value = action._value
        if name and context is not None:
            if hasattr(value, "perform"):
                try:
                    value = value.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    pass
            resolved_value = str(value) if value is not None else ""
            context._launch_configurations[str(name)] = resolved_value
            _state.tracked["set_launch_configurations"][str(name)] = resolved_value
        return

    # OpaqueFunction: execute its function and walk the result
    if isinstance(action, _TrackedOpaqueFunction) or (
        cls_name == "OpaqueFunction"
        or (hasattr(action, "function") and callable(getattr(action, "function", None)))
    ):
        fn = getattr(action, "function", None)
        if fn and context:
            try:
                result = _call_opaque_with_stubs(fn, context)
                if result:
                    _walk_actions(result, context, depth + 1)
            except _PackageNotFetchedError as e:
                _error(f"OpaqueFunction failed: package fetch failed: {e}")
            except Exception as e:
                _error(f"OpaqueFunction failed: {e}")
        return

    # SetEnvironmentVariable: mutate the env map (respects condition)
    if isinstance(action, _TrackedSetEnvironmentVariable):
        if action._condition is not None and context is not None:
            try:
                if not action._condition.evaluate(context):
                    return
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"SetEnvironmentVariable condition evaluation failed: {e}")
                return
        name = _to_str(action._name, context)
        if not name:
            _error("SetEnvironmentVariable: resolved name is empty or None — skipping")
            return
        value = _to_str(action._value, context) or ""
        _state.env[name] = value
        return

    # UnsetEnvironmentVariable: remove from env map (respects condition)
    if isinstance(action, _TrackedUnsetEnvironmentVariable):
        if action._condition is not None and context is not None:
            try:
                if not action._condition.evaluate(context):
                    return
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"UnsetEnvironmentVariable condition evaluation failed: {e}")
                return
        name = _to_str(action._name, context)
        if not name:
            _error("UnsetEnvironmentVariable: resolved name is empty or None — skipping")
            return
        if name in os.environ:
            # In process env (cases 2 & 3) — can't unset baseline.
            _error(
                f"unset_env: '{name}' exists in the process env and cannot be unset. "
                f'Use SetEnvironmentVariable(name="{name}", value="") '
                "or a scoped group instead"
            )
        elif name in _state.env:
            # Case 1: override-only, no baseline to expose — safe to remove.
            del _state.env[name]
        else:
            # Case 4: not set anywhere.
            _error(f"unset_env: environment variable '{name}' is not set")
        return

    # GroupAction: walk child actions; scoped groups save/restore env + namespace
    if isinstance(action, _TrackedGroupAction):
        if action._condition is not None and context is not None:
            try:
                if not action._condition.evaluate(context):
                    return
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"GroupAction condition evaluation failed: {e}")
                return
        depth_before = len(_state.namespace_stack)
        saved_env = dict(_state.env) if action._scoped else None
        _walk_actions(action._actions, context, depth + 1)
        del _state.namespace_stack[depth_before:]
        if saved_env is not None:
            _state.env.clear()
            _state.env.update(saved_env)
        return

    if isinstance(action, _TimerAction):
        _walk_actions(action._actions, context, depth + 1)
        return

    if hasattr(action, "entities"):
        try:
            _walk_actions(action.entities, context, depth + 1)
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"failed to walk {cls_name}.entities: {e}")
    if hasattr(action, "_actions"):
        try:
            _walk_actions(action._actions, context, depth + 1)
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"failed to walk {cls_name}._actions: {e}")

    # Real Node/LifecycleNode from launch_ros (unpatched, e.g. from OpaqueFunction return)
    if cls_name in ("Node", "LifecycleNode") and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        _track_node_from_action(pkg, exe)
    elif cls_name == "ComposableNodeContainer" and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        name = getattr(action, "_name", None)
        _track_node_from_action(pkg, exe, name)
        descs = (
            getattr(
                action,
                "composable_node_descriptions",
                getattr(action, "_composable_node_descriptions", None),
            )
            or []
        )
        for desc in descs:
            _track_node_from_action(
                getattr(desc, "package", None),
                getattr(desc, "plugin", None),
                getattr(desc, "node_name", getattr(desc, "name", None)),
            )
    elif cls_name == "LoadComposableNodes":
        descs = (
            getattr(
                action,
                "_composable_node_descriptions",
                getattr(action, "composable_node_descriptions", None),
            )
            or []
        )
        for desc in descs:
            _track_node_from_action(
                getattr(desc, "package", None),
                getattr(desc, "plugin", None),
                getattr(desc, "node_name", getattr(desc, "name", None)),
            )

    # IncludeLaunchDescription (real)
    if cls_name == "IncludeLaunchDescription":
        src = getattr(action, "_launch_description_source", None)
        if src:
            loc = getattr(src, "location", None)
            if callable(loc) and context:
                try:
                    loc = loc(context)
                except _PackageNotFetchedError:
                    raise  # Let it propagate to the OpaqueFunction or top-level handler
                except Exception:
                    loc = None
            if loc:
                _track_include(loc)

    # Warn about action classes we do not recognise.  Any class not in the known set may
    # represent a node, include, or container that will be absent from the resolved output.
    if cls_name not in _KNOWN_ACTION_CLASSES:
        _warn(
            f"Unrecognised action type '{cls_name}' — any nodes or includes it "
            f"declares may not appear in the resolved output"
        )


# ─── Known action class names ─────────────────────────────────────────────────

# Class names that _walk_action handles explicitly or that are safe to skip.
# Any class not in this set triggers a warning so the user knows the output may
# be incomplete.  The set covers:
#   • Our shimmed tracking classes (prefixed with underscore or "Tracked")
#   • Real launch_ros classes handled via cls_name duck-typing
#   • ROS 2 lifecycle / event infrastructure that produces no nodes/includes
_KNOWN_ACTION_CLASSES: frozenset = frozenset(
    {
        # Shimmed tracking classes
        "_DeclaredArg",
        "_TrackedNode",
        "_TrackedLifecycleNode",
        "_TrackedComposableNodeContainer",
        "_TrackedLoadComposableNodes",
        "_TrackedPushRosNamespace",
        "_TrackedIncludeLaunchDescription",
        "_TrackedSetParameter",
        "_TrackedExecutable",
        "_SetLaunchConfiguration",
        "_TrackedOpaqueFunction",
        "_TrackedGroupAction",
        "_TrackedRegisterEventHandler",
        "_TrackedEmitEvent",
        "_TrackedOnProcessStart",
        "_TrackedOnProcessExit",
        "_TrackedOnStateTransition",
        "_TrackedOnShutdown",
        "_TimerAction",
        # Real launch_ros classes handled via cls_name fallback in _walk_action
        "Node",
        "LifecycleNode",
        "ComposableNodeContainer",
        "LoadComposableNodes",
        "IncludeLaunchDescription",
        # ROS 2 infrastructure — safe to skip (no nodes/includes produced)
        "LogInfo",
        "RegisterEventHandler",
        "EmitEvent",
        "Shutdown",
        "ExecuteProcess",
        "DeclareLaunchArgument",
        "SetLaunchConfiguration",
        "SetParameter",
        "PushRosNamespace",
        "ResetLaunchConfigurations",
        "AppendEnvironmentVariable",
        "SetEnvironmentVariable",
        "UnsetEnvironmentVariable",
        "OpaqueCoroutine",
        "OpaqueFunction",
        "GroupAction",
        "TimerAction",
    }
)

# ─── Import system patcher ────────────────────────────────────────────────────

_PATCHED_MODULES = {}


def _build_patched_launch():
    """Stub for the top-level `launch` package (used when ROS 2 is not installed)."""
    mod = types.ModuleType("launch")
    mod.__path__ = []  # mark as package so submodule imports work
    mod.__package__ = "launch"

    class _LaunchDescription:
        def __init__(self, entities=None, **kwargs):
            self.entities = list(entities or [])

        def add_action(self, action):
            self.entities.append(action)

    mod.LaunchDescription = _LaunchDescription
    mod.LaunchContext = _StubLaunchContext
    return mod


def _build_patched_launch_ros():
    """Stub for the top-level `launch_ros` package."""
    mod = types.ModuleType("launch_ros")
    mod.__path__ = []
    mod.__package__ = "launch_ros"
    return mod


def _build_patched_launch_ros_actions():
    mod = types.ModuleType("launch_ros.actions")
    mod.Node = _TrackedNode
    mod.LifecycleNode = _TrackedLifecycleNode
    mod.ComposableNodeContainer = _TrackedComposableNodeContainer
    mod.LoadComposableNodes = _TrackedLoadComposableNodes
    mod.SetParameter = _TrackedSetParameter
    mod.SetRemap = lambda *a, **kw: None
    mod.PushRosNamespace = _TrackedPushRosNamespace
    mod.SetParametersCallback = lambda *a, **kw: None
    return mod


def _build_patched_launch_ros_utilities():
    mod = types.ModuleType("launch_ros.utilities")

    def make_namespace_absolute(namespace):
        """Ensure namespace starts with /."""
        ns = str(namespace) if namespace else ""
        if ns and not ns.startswith("/"):
            return "/" + ns
        return ns or "/"

    def prefix_namespace(namespace, name):
        """Prepend namespace to name."""
        ns = str(namespace).rstrip("/") if namespace else ""
        n = str(name).lstrip("/") if name else ""
        if not ns:
            return n
        if not n:
            return ns
        return ns + "/" + n

    mod.make_namespace_absolute = make_namespace_absolute
    mod.prefix_namespace = prefix_namespace
    mod.get_node_name_count = lambda *a, **kw: 0
    mod.evaluate_parameters = lambda *a, **kw: []
    mod.normalize_parameters = lambda *a, **kw: []
    mod.add_node_name_count_to_name = lambda name, **kw: name
    return mod


def _build_patched_launch_ros_descriptions():
    mod = types.ModuleType("launch_ros.descriptions")
    mod.ComposableNode = _TrackedComposableNode
    mod.ParameterFile = _TrackedParameterFile
    return mod


def _build_patched_launch_substitutions():
    """Always-stub: avoids recursion since we also patch top-level `launch`."""
    mod = types.ModuleType("launch.substitutions")
    mod.__path__ = []  # mark as package so submodule imports work
    mod.FindPackageShare = _TrackedFindPackageShare
    mod.PathJoinSubstitution = _TrackedPathJoinSubstitution
    mod.LaunchConfiguration = _LaunchConfiguration
    _SENTINEL = object()

    class _DeferredEnvironmentVariable:
        """Deferred substitution: reads _state.env at perform() time, not construction."""

        def __init__(self, name, **kw):
            self._name = name
            self._default = kw.get("default_value", _SENTINEL)

        def perform(self, context=None):
            # Resolve name to a concrete string via _to_str.
            name = _to_str(self._name, context) or ""
            # Look up in override env, then process env.
            if name in _state.env:
                return _state.env[name]
            if name in os.environ:
                return os.environ[name]
            # No match — use default if provided, otherwise error.
            if self._default is not _SENTINEL:
                return _to_str(self._default, context) or ""
            _error(f"EnvironmentVariable: '{name}' is not set and no default was provided")
            return ""

        def __str__(self):
            # Avoid calling perform() without context — return the raw name.
            return str(self._name) if self._name is not None else ""

    mod.EnvironmentVariable = _DeferredEnvironmentVariable
    mod.TextSubstitution = lambda text="", **kw: str(text)
    mod.PythonExpression = lambda expression=None, **kw: None
    mod.ThisLaunchFileDir = lambda: Path(__file__).parent
    return mod


def _build_patched_launch_substitutions_environment_variable():
    """Submodule stub for ``from launch.substitutions.environment_variable import ...``."""
    parent = sys.modules.get("launch.substitutions")
    if parent is None:
        parent = _build_patched_launch_substitutions()
    mod = types.ModuleType("launch.substitutions.environment_variable")
    mod.EnvironmentVariable = parent.EnvironmentVariable
    return mod


def _build_patched_launch_actions():
    """Always-stub: avoids recursion since we also patch top-level `launch`."""
    mod = types.ModuleType("launch.actions")
    mod.IncludeLaunchDescription = _TrackedIncludeLaunchDescription
    mod.DeclareLaunchArgument = _DeclaredArg
    mod.OpaqueFunction = _TrackedOpaqueFunction
    mod.GroupAction = _TrackedGroupAction
    mod.SetLaunchConfiguration = _SetLaunchConfiguration
    mod.LogInfo = lambda *a, **kw: None
    mod.TimerAction = _TimerAction
    mod.RegisterEventHandler = _TrackedRegisterEventHandler
    mod.EmitEvent = _TrackedEmitEvent
    mod.Shutdown = _TrackedShutdown
    mod.PushLaunchConfigurations = lambda *a, **kw: None
    mod.PopLaunchConfigurations = lambda *a, **kw: None
    mod.SetEnvironmentVariable = _TrackedSetEnvironmentVariable
    mod.UnsetEnvironmentVariable = _TrackedUnsetEnvironmentVariable
    mod.ExecuteProcess = _TrackedExecutable
    mod.ExecuteLocal = lambda *a, **kw: None
    mod.OnProcessExit = _TrackedOnProcessExit
    mod.OnProcessStart = _TrackedOnProcessStart
    return mod


def _build_patched_launch_event_handlers():
    """Tracked stubs for launch.event_handlers — record event handler structure."""
    mod = types.ModuleType("launch.event_handlers")
    mod.OnProcessExit = _TrackedOnProcessExit
    mod.OnProcessStart = _TrackedOnProcessStart
    mod.OnProcessIO = lambda *a, **kw: None
    mod.OnShutdown = _TrackedOnShutdown
    mod.OnStateTransition = _TrackedOnStateTransition
    mod.OnExecutionComplete = lambda *a, **kw: None
    return mod


def _build_patched_launch_conditions():
    mod = types.ModuleType("launch.conditions")

    class _IfCondition:
        """Stub for IfCondition that supports .evaluate(context)."""

        def __init__(self, condition=None, **kwargs):
            self._condition = condition

        def evaluate(self, context):
            try:
                if hasattr(self._condition, "perform"):
                    val = str(self._condition.perform(context)).lower()
                    return val in ("true", "1", "yes", "on")
                if self._condition is None:
                    return False
                return bool(self._condition)
            except Exception:
                return False

    class _UnlessCondition(_IfCondition):
        """Stub for UnlessCondition: evaluate() returns the logical inverse of IfCondition."""

        def evaluate(self, context):
            return not super().evaluate(context)

    class _LaunchConfigurationEquals:
        def __init__(self, name=None, expected_value=None, **kwargs):
            self._name = name
            self._expected = expected_value

        def evaluate(self, context):
            try:
                val = context._launch_configurations.get(str(self._name), "")
                return str(val) == str(self._expected)
            except Exception:
                return False

    class _LaunchConfigurationNotEquals(_LaunchConfigurationEquals):
        def evaluate(self, context):
            return not super().evaluate(context)

    mod.IfCondition = _IfCondition
    mod.UnlessCondition = _UnlessCondition
    mod.LaunchConfigurationEquals = _LaunchConfigurationEquals
    mod.LaunchConfigurationNotEquals = _LaunchConfigurationNotEquals
    return mod


def _build_patched_launch_launch_description_sources():
    mod = types.ModuleType("launch.launch_description_sources")

    class _PythonLaunchDescriptionSource:
        def __init__(self, location=None, **kwargs):
            # Resolve to a string path.  Location may be:
            #   - None
            #   - A substitution object with .perform()  (e.g. PathJoinSubstitution)
            #   - A list of strings/substitutions to concatenate
            #     (e.g. [FindPackageShare("pkg"), "/launch/file.py"])
            #   - A plain string
            if location is None:
                self._location = None
            elif hasattr(location, "perform"):
                try:
                    result = location.perform(_StubLaunchContext())
                    self._location = str(result) if result is not None else str(location)
                except Exception:
                    self._location = None
            elif isinstance(location, list):
                stub_ctx = _StubLaunchContext()
                parts = []
                for sub in location:
                    if hasattr(sub, "perform"):
                        try:
                            result = sub.perform(stub_ctx)
                            parts.append(str(result) if result is not None else str(sub))
                        except Exception:
                            parts.append(str(sub))
                    else:
                        parts.append(str(sub))
                self._location = "".join(parts)
            else:
                self._location = str(location)

    class _AnyLaunchDescriptionSource(_PythonLaunchDescriptionSource):
        pass

    mod.PythonLaunchDescriptionSource = _PythonLaunchDescriptionSource
    mod.AnyLaunchDescriptionSource = _AnyLaunchDescriptionSource
    return mod


def _build_patched_launch_ros_substitutions():
    """launch_ros.substitutions — provides FindPackageShare (same as launch.substitutions)."""
    mod = types.ModuleType("launch_ros.substitutions")
    mod.FindPackageShare = _TrackedFindPackageShare
    return mod


def _build_patched_launch_ros_parameter_descriptions():
    mod = types.ModuleType("launch_ros.parameter_descriptions")
    mod.ParameterFile = _TrackedParameterFile
    mod.ParameterDescription = lambda *a, **kw: None
    mod.ParameterValue = lambda *a, **kw: None
    return mod


def _build_patched_ament_index_python():
    mod = types.ModuleType("ament_index_python")
    mod.__path__ = []
    mod.__package__ = "ament_index_python"
    return mod


def _build_patched_ament_index_python_packages():
    mod = types.ModuleType("ament_index_python.packages")

    def get_package_share_directory(package_name):
        _warn(
            f"get_package_share_directory('{package_name}') is non-idiomatic; "
            f"prefer FindPackageShare('{package_name}') from launch_ros.substitutions"
        )
        _track_package(package_name)
        # Early fetch check using the actual package dir (before returning a portable path).
        # This ensures that if the package is in the lockfile but hasn't been fully fetched
        # yet (no package.xml), we fetch it inline — important for module-level callers
        # where os.path stubs may not be active.
        if package_name in _state.package_shares:
            pkg_dir = _state.package_shares[package_name]
            if not os.path.isfile(os.path.join(pkg_dir, "package.xml")) and not _ensure_fetched(
                package_name
            ):
                raise _PackageNotFetchedError(package_name)
        elif package_name in _state.lockfile_data:
            if not _ensure_fetched(package_name):
                raise _PackageNotFetchedError(package_name)
        # In non-preview mode, return the real install path so the output contains
        # absolute paths matching the installed layout.
        if not _state.preview_mode and package_name in _state.package_shares:
            return _state.package_shares[package_name]
        # In preview mode, return a portable path so that derived paths (e.g.
        # os.path.join(share_dir, "calib/")) stay portable.  Inside
        # _call_opaque_with_stubs the os.path.* stubs intercept any filesystem
        # access and resolve the portable path lazily.
        return f"$(find-pkg-share {package_name})"

    def get_package_prefix(package_name):
        # Return the parent of the share directory as a best-effort prefix.
        # For unresolved packages the share path is portable syntax like
        # "$(find-pkg-share pkg)" — fall back to the ROS distro prefix.
        share = _resolve_pkg_share(package_name)
        if share.startswith("$("):
            return _ROS_DISTRO_PREFIX
        p = Path(share)
        return str(p.parent) if p.parent != p else _ROS_DISTRO_PREFIX

    mod.get_package_share_directory = get_package_share_directory
    mod.get_package_prefix = get_package_prefix
    return mod


def _build_patched_launch_ros_events():
    """Patched launch_ros.events — provides ChangeState."""
    mod = types.ModuleType("launch_ros.events")
    mod.__path__ = []
    mod.__package__ = "launch_ros.events"
    mod.ChangeState = _TrackedChangeState
    return mod


def _build_patched_launch_ros_events_lifecycle():
    """Patched launch_ros.events.lifecycle — provides ChangeState."""
    mod = types.ModuleType("launch_ros.events.lifecycle")
    mod.ChangeState = _TrackedChangeState
    return mod


def _build_patched_launch_ros_event_handlers():
    """Patched launch_ros.event_handlers — lifecycle event matchers."""
    mod = types.ModuleType("launch_ros.event_handlers")
    mod.__path__ = []
    mod.__package__ = "launch_ros.event_handlers"
    mod.OnStateTransition = _TrackedOnStateTransition
    return mod


def _build_patched_launch_ros_event_handlers_on_state_transition():
    mod = types.ModuleType("launch_ros.event_handlers.on_state_transition")
    mod.OnStateTransition = _TrackedOnStateTransition
    return mod


def _build_patched_lifecycle_msgs():
    """Patched lifecycle_msgs — provides Transition constants."""
    mod = types.ModuleType("lifecycle_msgs")
    mod.__path__ = []
    mod.__package__ = "lifecycle_msgs"
    return mod


def _build_patched_lifecycle_msgs_msg():
    """Patched lifecycle_msgs.msg — provides Transition constants."""
    mod = types.ModuleType("lifecycle_msgs.msg")

    class Transition:
        TRANSITION_CONFIGURE = 1
        TRANSITION_CLEANUP = 2
        TRANSITION_ACTIVATE = 3
        TRANSITION_DEACTIVATE = 4
        TRANSITION_UNCONFIGURED_SHUTDOWN = 5
        TRANSITION_INACTIVE_SHUTDOWN = 6
        TRANSITION_ACTIVE_SHUTDOWN = 7

    mod.Transition = Transition
    return mod


def _build_patched_launch_events():
    """Patched launch.events — provides Shutdown event and matches_action."""
    mod = types.ModuleType("launch.events")
    mod.__path__ = []
    mod.__package__ = "launch.events"
    mod.Shutdown = _TrackedShutdown
    mod.matches_action = _TrackedMatchesAction
    return mod


def _build_patched_launch_events_process():
    mod = types.ModuleType("launch.events.process")
    return mod


def _build_patched_launch_event_handler():
    mod = types.ModuleType("launch.event_handler")
    mod.EventHandler = lambda *a, **kw: None
    return mod


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Intercept imports of specific launch/ament modules and return patched versions."""

    PATCHED = {
        "launch": _build_patched_launch,
        "launch_ros": _build_patched_launch_ros,
        "launch_ros.actions": _build_patched_launch_ros_actions,
        "launch_ros.descriptions": _build_patched_launch_ros_descriptions,
        "launch_ros.substitutions": _build_patched_launch_ros_substitutions,
        "launch_ros.parameter_descriptions": _build_patched_launch_ros_parameter_descriptions,
        "launch_ros.utilities": _build_patched_launch_ros_utilities,
        "launch.substitutions": _build_patched_launch_substitutions,
        "launch.substitutions.environment_variable": _build_patched_launch_substitutions_environment_variable,
        "launch.actions": _build_patched_launch_actions,
        "launch.event_handlers": _build_patched_launch_event_handlers,
        "launch.conditions": _build_patched_launch_conditions,
        "launch.launch_description_sources": _build_patched_launch_launch_description_sources,
        "ament_index_python": _build_patched_ament_index_python,
        "ament_index_python.packages": _build_patched_ament_index_python_packages,
        "launch_ros.events": _build_patched_launch_ros_events,
        "launch_ros.events.lifecycle": _build_patched_launch_ros_events_lifecycle,
        "launch_ros.event_handlers": _build_patched_launch_ros_event_handlers,
        "launch_ros.event_handlers.on_state_transition": _build_patched_launch_ros_event_handlers_on_state_transition,
        "lifecycle_msgs": _build_patched_lifecycle_msgs,
        "lifecycle_msgs.msg": _build_patched_lifecycle_msgs_msg,
        "launch.events": _build_patched_launch_events,
        "launch.events.process": _build_patched_launch_events_process,
        "launch.event_handler": _build_patched_launch_event_handler,
    }

    def find_spec(self, fullname, path, target=None):
        if fullname in self.PATCHED:
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        if spec.name not in _PATCHED_MODULES:
            _PATCHED_MODULES[spec.name] = self.PATCHED[spec.name]()
        return _PATCHED_MODULES[spec.name]

    def exec_module(self, module):
        pass  # Already fully initialised in create_module


# ─── Main ─────────────────────────────────────────────────────────────────────


def _emit(obj):
    """Write JSON to the real stdout (saved before redirect)."""
    _emit._fd.write(json.dumps(obj))
    _emit._fd.write("\n")
    _emit._fd.flush()


def main():
    # Redirect stdout → stderr so that print() calls inside user launch files
    # (e.g. OpaqueFunction bodies) don't contaminate our JSON output.
    _emit._fd = sys.stdout
    sys.stdout = sys.stderr

    # Read input from stdin (JSON object with launch_file, args, package_shares, flags).
    # This avoids OS ARG_MAX limits that occur with large lockfiles.
    stdin_data = json.loads(sys.stdin.read())
    launch_file = stdin_data["launch_file"]
    args_dict = stdin_data.get("args", {})

    _state.namespace_stack = []
    # Set root source key for per-file declared arg tracking
    root_dep = _extract_pkg_and_share_path(launch_file)
    _state.root_source_key = f"{root_dep[0]}://{root_dep[1]}" if root_dep else launch_file
    _state.env.clear()
    _state.global_params.clear()
    _state.global_remaps.clear()
    _state.global_param_files.clear()
    _state.fetched_packages.clear()
    _state.package_shares = stdin_data.get("package_shares", {})

    # Load workflow flags.
    flags = stdin_data.get("flags", {})
    _state.apply_opaque_file_access = bool(flags.get("apply_opaque_file_access", False))
    _state.preview_mode = bool(flags.get("preview", True))
    _state.inline_params = bool(flags.get("inline_params", False))
    _state.rosdep_fallback = bool(flags.get("rosdep_fallback", False))
    _state.apply_arg_defaults = bool(flags.get("apply_arg_defaults", False))
    _state.global_arg_cascade = bool(flags.get("global_arg_cascade", False))
    _state.allow_unportable_paths = bool(flags.get("allow_unportable_paths", False))
    # lockfile_packages: dict of pkg → {repo, path, url, version}
    lf_data = flags.get("lockfile_packages", {})
    _state.lockfile_data = lf_data if isinstance(lf_data, dict) else {}
    _state.fetch_dir = flags.get("fetch_dir", "")

    # Arg values may contain portable paths ($(find-pkg-share pkg)/...) when they are
    # cascaded from the XML resolver.  These are intentionally preserved as-is: the
    # _stub_open and os.path stubs inside _call_opaque_with_stubs handle them lazily,
    # resolving only when the OpaqueFunction actually accesses the filesystem.

    # Install the import patcher before anything imports launch.
    # Also force our stubs into sys.modules to displace any already-imported real modules.
    # Python checks sys.modules before consulting meta path finders, so the real
    # ament_index_python.packages (imported above for _real_get_package_share_directory)
    # would otherwise bypass our stub entirely.
    sys.meta_path.insert(0, _PatchingFinder())
    for mod_name, builder in _PatchingFinder.PATCHED.items():
        if mod_name not in _PATCHED_MODULES:
            _PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = _PATCHED_MODULES[mod_name]

    # ── XML / YAML files: parse and walk with the flat walker ──────────────
    if launch_file.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
        try:
            with open(launch_file) as f:
                content = f.read()
        except Exception as e:
            _error(f"cannot read {launch_file}: {e}")
            _emit(_state.tracked)
            return
        if launch_file.endswith((".yaml", ".yml")):
            elements = parse_yaml_launch(content, launch_file)
        else:
            elements = parse_xml_launch(content, launch_file)
        subst_ctx = _SubstitutionContext()
        subst_ctx.args = dict(args_dict)
        subst_ctx.launch_file_dir = os.path.dirname(os.path.abspath(launch_file))
        subst_ctx.preview_mode = _state.preview_mode
        subst_ctx.env = dict(_state.env)
        try:
            resolve_xml_elements(elements, subst_ctx, include_stack=[launch_file])
        except _PackageNotFetchedError as e:
            _error(f"failed to fetch package: {e}")
        _emit(_state.tracked)
        return

    # ── Python launch files: load as module ──────────────────────────────
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file)
    if spec is None:
        _emit({"error": f"cannot load {launch_file}"})
        sys.exit(1)

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except _PackageNotFetchedError as e:
        _error(f"failed to fetch package during module load: {e}")
        _emit(_state.tracked)
        return
    except Exception as e:
        _error(f"Error loading launch file: {e}")
        _emit(_state.tracked)
        return

    # Call generate_launch_description()
    if not hasattr(mod, "generate_launch_description"):
        _error("No generate_launch_description() function found")
        _emit(_state.tracked)
        return

    # Pre-populate global_params from the orchestrator's persisted context so that
    # files that read context.launch_configurations["global_params"] (e.g. to access
    # vehicle dimensions set by SetParameter in a sibling file) find the data already
    # there.  The key is stripped from args_dict before building the launch context.
    persisted_global_params = []
    if "__global_params__" in args_dict:
        try:
            persisted_global_params = json.loads(args_dict.pop("__global_params__"))
        except Exception:
            pass

    ctx = _make_launch_context(args_dict)

    if persisted_global_params:
        # Store as list of (name, value) tuples matching real ROS 2 SetParameter layout.
        gp_tuples = [(entry[0], entry[1]) for entry in persisted_global_params if len(entry) == 2]
        ctx._launch_configurations["global_params"] = list(gp_tuples)
        # Also populate _state.global_params so _resolve_node_details merges them into nodes.
        _state.global_params.extend(gp_tuples)

    try:
        ld = mod.generate_launch_description()
    except _PackageNotFetchedError as e:
        _error(f"failed to fetch package during generate_launch_description: {e}")
        _emit(_state.tracked)
        return
    except Exception as e:
        _error(f"generate_launch_description() failed: {e}")
        _emit(_state.tracked)
        return

    # Walk the LaunchDescription tree — two passes, mirroring the XML resolver's behavior:
    #
    # Pass 1 (sequential): Apply all top-level DeclareLaunchArgument defaults to the context
    #   in list order.  This populates ctx._launch_configurations so that a later declaration
    #   can reference an earlier one via LaunchConfiguration("prev_arg"), exactly as the XML
    #   resolver processes <arg default="..."> elements sequentially into ctx.args.
    #
    # Pass 2: Walk all actions.  OpaqueFunction bodies now have a fully populated context.
    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, _DeclaredArg):
            _apply_declared_arg(entity, ctx)

    try:
        _walk_actions(entities, ctx)
    except _PackageNotFetchedError as e:
        _error(f"failed to fetch package during action walking: {e}")

    _emit(_state.tracked)


def resolve_file(
    launch_file: "Path",
    args: dict[str, str],
    package_shares: dict[str, str],
    workflow_options: Any = None,
    lockfile: Any = None,
    fetch_dir: "Path | None" = None,
    global_params: list | None = None,
) -> Any:
    """Public API: resolve a launch file and return a ParsedLaunchFile.

    This replaces the stdin/stdout JSON boundary used by the Rust orchestrator.
    Calls the same internal logic as ``main()`` but returns structured data
    instead of writing JSON to stdout.

    Parameters
    ----------
    launch_file : Path
        Absolute path to the launch file.
    args : dict
        Launch arguments (``name:=value`` pairs).
    package_shares : dict
        Package name → share directory path.
    workflow_options : ResolveWorkflowOptions
        Workflow flags (from orchestrator).
    lockfile : Lockfile
        Lockfile with package/repo info.
    fetch_dir : Path | None
        Directory for sparse-checkout.
    global_params : list | None
        Persisted global params from prior files.

    Returns
    -------
    ParsedLaunchFile
        The resolver result as a structured object (imported from types module).
    """
    # ── Set up globals (same as main()) ──────────────────────────────────

    launch_file_str = str(launch_file)

    _state.namespace_stack = []
    root_dep = _extract_pkg_and_share_path(launch_file_str)
    _state.root_source_key = f"{root_dep[0]}://{root_dep[1]}" if root_dep else launch_file_str

    _state.env.clear()
    _state.global_params.clear()
    _state.global_remaps.clear()
    _state.global_param_files.clear()
    _state.fetched_packages.clear()
    _state.declared_arg_names.clear()
    _state.include_chain.clear()
    _state.package_shares = dict(package_shares)

    # Reset _state.tracked
    _state.tracked["packages"] = []
    _state.tracked["includes"] = []
    _state.tracked["nodes"] = []
    _state.tracked["warnings"] = []
    _state.tracked["errors"] = []
    _state.tracked["declared_args"] = []
    _state.tracked["declared_args_by_file"] = {}
    _state.tracked["global_params"] = []
    _state.tracked["include_args"] = {}
    _state.tracked["param_files"] = []
    _state.tracked["set_launch_configurations"] = {}
    _state.tracked["include_deps"] = []
    _state.tracked["param_file_deps"] = []
    _state.tracked["event_handlers"] = []

    # Workflow flags
    if workflow_options is not None:
        _state.apply_opaque_file_access = bool(
            getattr(workflow_options, "apply_opaque_file_access", False)
        )
        _state.preview_mode = bool(getattr(workflow_options, "preview", True))
        _state.inline_params = bool(getattr(workflow_options, "inline_params", False))
        _state.rosdep_fallback = bool(getattr(workflow_options, "rosdep_fallback", False))
        _state.apply_arg_defaults = bool(getattr(workflow_options, "apply_arg_defaults", False))
        _state.global_arg_cascade = bool(getattr(workflow_options, "global_arg_cascade", False))
        _state.allow_unportable_paths = bool(
            getattr(workflow_options, "allow_unportable_paths", False)
        )
    else:
        _state.apply_opaque_file_access = False
        _state.preview_mode = True
        _state.inline_params = False
        _state.rosdep_fallback = False
        _state.apply_arg_defaults = False
        _state.global_arg_cascade = False
        _state.allow_unportable_paths = False

    # Build lockfile data from the Lockfile dataclass
    if lockfile is not None:
        lf_data = {}
        for pkg_name, pkg_lock in lockfile.packages.items():
            repo_lock = lockfile.repositories.get(pkg_lock.repo)
            if repo_lock is not None:
                lf_data[pkg_name] = {
                    "repo": pkg_lock.repo,
                    "path": pkg_lock.path,
                    "url": repo_lock.url,
                    "version": repo_lock.version,
                }
        _state.lockfile_data = lf_data
    else:
        _state.lockfile_data = {}

    _state.fetch_dir = str(fetch_dir) if fetch_dir else ""

    args_dict = dict(args)

    # Pre-populate global params
    persisted_global_params = list(global_params) if global_params else []

    # Install the import patcher
    sys.meta_path.insert(0, _PatchingFinder())
    for mod_name, builder in _PatchingFinder.PATCHED.items():
        if mod_name not in _PATCHED_MODULES:
            _PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = _PATCHED_MODULES[mod_name]

    # ── Resolve by file type ─────────────────────────────────────────────
    if launch_file_str.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
        try:
            with open(launch_file_str) as f:
                content = f.read()
        except Exception as e:
            _error(f"cannot read {launch_file_str}: {e}")
            return _tracked_to_parsed_launch_file(_state.tracked)

        if launch_file_str.endswith((".yaml", ".yml")):
            elements = parse_yaml_launch(content, launch_file_str)
        else:
            elements = parse_xml_launch(content, launch_file_str)
        subst_ctx = _SubstitutionContext()
        subst_ctx.args = dict(args_dict)
        subst_ctx.launch_file_dir = os.path.dirname(os.path.abspath(launch_file_str))
        subst_ctx.preview_mode = _state.preview_mode
        subst_ctx.env = dict(_state.env)
        try:
            resolve_xml_elements(elements, subst_ctx, include_stack=[launch_file_str])
        except _PackageNotFetchedError as e:
            _error(f"failed to fetch package: {e}")
        return _tracked_to_parsed_launch_file(_state.tracked)

    # ── Python launch files ──────────────────────────────────────────────
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file_str)
    if spec is None or spec.loader is None:
        _error(f"cannot load {launch_file_str}")
        return _tracked_to_parsed_launch_file(_state.tracked)

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except _PackageNotFetchedError as e:
        _error(f"failed to fetch package during module load: {e}")
        return _tracked_to_parsed_launch_file(_state.tracked)
    except Exception as e:
        _error(f"Error loading launch file: {e}")
        return _tracked_to_parsed_launch_file(_state.tracked)

    if not hasattr(mod, "generate_launch_description"):
        _error("No generate_launch_description() function found")
        return _tracked_to_parsed_launch_file(_state.tracked)

    # Inject persisted global params
    if "__global_params__" in args_dict:
        try:
            persisted_global_params = json.loads(args_dict.pop("__global_params__"))
        except Exception:
            pass

    ctx = _make_launch_context(args_dict)

    if persisted_global_params:
        gp_tuples = [(entry[0], entry[1]) for entry in persisted_global_params if len(entry) == 2]
        ctx._launch_configurations["global_params"] = list(gp_tuples)
        _state.global_params.extend(gp_tuples)

    try:
        ld = mod.generate_launch_description()
    except _PackageNotFetchedError as e:
        _error(f"failed to fetch package during generate_launch_description: {e}")
        return _tracked_to_parsed_launch_file(_state.tracked)
    except Exception as e:
        _error(f"generate_launch_description() failed: {e}")
        return _tracked_to_parsed_launch_file(_state.tracked)

    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, _DeclaredArg):
            _apply_declared_arg(entity, ctx)

    try:
        _walk_actions(entities, ctx)
    except _PackageNotFetchedError as e:
        _error(f"failed to fetch package during action walking: {e}")

    return _tracked_to_parsed_launch_file(_state.tracked)


def _tracked_to_parsed_launch_file(tracked: dict[str, Any]) -> Any:
    """Convert the internal _state.tracked dict to a ParsedLaunchFile."""
    from pathlib import Path as _Path

    from launch_plus.types import (
        ComposablePlugin as _ComposablePlugin,
    )
    from launch_plus.types import (
        DependencyKind as _DependencyKind,
    )
    from launch_plus.types import (
        EventHandlerKind as _EventHandlerKind,
    )
    from launch_plus.types import (
        FileDependency as _FileDependency,
    )
    from launch_plus.types import (
        LaunchInclude as _LaunchInclude,
    )
    from launch_plus.types import (
        NodeKindTag as _NodeKindTag,
    )
    from launch_plus.types import (
        ParamFileInlined as _ParamFileInlined,
    )
    from launch_plus.types import (
        ParamFileReference as _ParamFileReference,
    )
    from launch_plus.types import (
        ParsedLaunchFile as _ParsedLaunchFile,
    )
    from launch_plus.types import (
        ResolvedEventAction as _ResolvedEventAction,
    )
    from launch_plus.types import (
        ResolvedNode as _ResolvedNode,
    )
    from launch_plus.types import (
        effective_namespace as _effective_namespace,
    )

    # ── Convert nodes ────────────────────────────────────────────────────
    resolved_nodes: list[_ResolvedNode] = []
    for n in tracked.get("nodes", []):
        kind_str = n.get("kind", "node")
        if kind_str == "set_parameter":
            continue

        ns_stack = n.get("namespace_stack", [])
        explicit_ns = n.get("explicit_namespace")
        eff_ns = _effective_namespace(ns_stack, explicit_ns)
        name = n.get("name", "")

        # Map kind
        kind_tag: _NodeKindTag
        plugins: list[_ComposablePlugin] = []
        load_target: str | None = None
        log_message: str | None = None
        remap_from: str | None = None
        remap_to: str | None = None
        exec_cmd: str | None = None
        exec_name: str | None = None
        exec_shell: bool = False
        handler_kind: _EventHandlerKind | None = None
        handler_target: str | None = None
        handler_target_node: str | None = None
        handler_namespace: str | None = None
        handler_start_state: str | None = None
        handler_goal_state: str | None = None
        handler_actions: list[_ResolvedEventAction] = []

        if kind_str == "container":
            kind_tag = _NodeKindTag.CONTAINER
            plugins = _convert_plugins(n.get("plugins", []))
        elif kind_str == "load_composable":
            kind_tag = _NodeKindTag.LOAD_COMPOSABLE
            load_target = n.get("target", "")
            plugins = _convert_plugins(n.get("plugins", []))
        elif kind_str == "lifecycle_node":
            kind_tag = _NodeKindTag.LIFECYCLE_NODE
        elif kind_str == "set_remap":
            kind_tag = _NodeKindTag.SET_REMAP
            remap_from = n.get("remap_from", "")
            remap_to = n.get("remap_to", "")
        elif kind_str == "log":
            kind_tag = _NodeKindTag.LOG
            log_message = n.get("message", "")
        elif kind_str == "executable":
            kind_tag = _NodeKindTag.EXECUTABLE
            exec_cmd = n.get("cmd", "")
            exec_name = name if name else None
            exec_shell = n.get("shell", False)
        elif kind_str == "event_handler":
            kind_tag = _NodeKindTag.EVENT_HANDLER
            hk_str = n.get("handler_kind", "")
            hk_map = {
                "on_process_start": _EventHandlerKind.ON_PROCESS_START,
                "on_process_exit": _EventHandlerKind.ON_PROCESS_EXIT,
                "on_state_transition": _EventHandlerKind.ON_STATE_TRANSITION,
                "on_shutdown": _EventHandlerKind.ON_SHUTDOWN,
            }
            handler_kind = hk_map.get(hk_str)
            if handler_kind is None:
                continue
            handler_target = n.get("target")
            handler_target_node = n.get("target_node")
            handler_namespace = _effective_namespace(ns_stack, explicit_ns)
            handler_start_state = n.get("start_state")
            handler_goal_state = n.get("goal_state")
            handler_actions = [
                _ResolvedEventAction(
                    event=a.get("event", ""),
                    target_node=a.get("target_node"),
                    namespace=a.get("explicit_namespace"),
                )
                for a in n.get("eh_actions", [])
            ]
        else:
            kind_tag = _NodeKindTag.NODE

        # Include chain
        chain_raw = n.get("include_chain", [])
        include_chain = [(pkg, _Path(sp)) for pkg, sp in chain_raw]
        source = include_chain[-1] if include_chain else None

        # Param files
        param_files: list[_ParamFileReference | _ParamFileInlined] = []
        for pf in n.get("param_files", []):
            if pf.get("params") is not None:
                param_files.append(_ParamFileInlined(display=pf["path"], params=pf["params"]))
            else:
                param_files.append(_ParamFileReference(display=pf["path"], abs=pf["path"]))

        resolved_nodes.append(
            _ResolvedNode(
                package=n.get("package", ""),
                executable=n.get("executable", ""),
                name=name if name else None,
                namespace=eff_ns,
                explicit_namespace=explicit_ns,
                namespace_stack=list(ns_stack),
                parameters=dict(n.get("parameters", {})),
                remappings=[(f, t) for f, t in n.get("remappings", [])],
                env=dict(n.get("env", {})),
                source=source,
                include_chain=include_chain,
                kind=kind_tag,
                plugins=plugins,
                load_target=load_target,
                log_message=log_message,
                remap_from=remap_from,
                remap_to=remap_to,
                exec_cmd=exec_cmd,
                exec_name=exec_name,
                exec_shell=exec_shell,
                handler_kind=handler_kind,
                handler_target=handler_target,
                handler_target_node=handler_target_node,
                handler_namespace=handler_namespace,
                handler_start_state=handler_start_state,
                handler_goal_state=handler_goal_state,
                handler_actions=handler_actions,
                param_files=param_files,
                output=n.get("output"),
                args=n.get("args"),
                respawn=n.get("respawn"),
                respawn_delay=n.get("respawn_delay"),
            )
        )

    # ── Legacy event handlers ────────────────────────────────────────────
    has_inline_eh = any(n.kind == _NodeKindTag.EVENT_HANDLER for n in resolved_nodes)
    if not has_inline_eh:
        hk_map = {
            "on_process_start": _EventHandlerKind.ON_PROCESS_START,
            "on_process_exit": _EventHandlerKind.ON_PROCESS_EXIT,
            "on_state_transition": _EventHandlerKind.ON_STATE_TRANSITION,
            "on_shutdown": _EventHandlerKind.ON_SHUTDOWN,
        }
        for eh in tracked.get("event_handlers", []):
            hk = hk_map.get(eh.get("handler_kind", ""))
            if hk is None:
                continue
            ns_stack = eh.get("namespace_stack", [])
            explicit_ns = eh.get("explicit_namespace")
            handler_ns = _effective_namespace(ns_stack, explicit_ns)
            actions = [
                _ResolvedEventAction(
                    event=a.get("event", ""),
                    target_node=a.get("target_node"),
                    namespace=a.get("explicit_namespace"),
                )
                for a in eh.get("actions", [])
            ]
            resolved_nodes.append(
                _ResolvedNode(
                    kind=_NodeKindTag.EVENT_HANDLER,
                    namespace_stack=list(ns_stack),
                    explicit_namespace=explicit_ns,
                    handler_kind=hk,
                    handler_target=eh.get("target"),
                    handler_target_node=eh.get("target_node"),
                    handler_namespace=handler_ns,
                    handler_start_state=eh.get("start_state"),
                    handler_goal_state=eh.get("goal_state"),
                    handler_actions=actions,
                )
            )

    # ── Convert include deps ─────────────────────────────────────────────
    launch_includes: list[_LaunchInclude] = []
    include_args_map = tracked.get("include_args", {})
    for dep in tracked.get("include_deps", []):
        dep_include_args = dep.get("include_args", {})
        if not dep_include_args:
            dep_include_args = include_args_map.get(dep.get("path", ""), {})
        launch_includes.append(
            _LaunchInclude(
                package=dep["package"],
                share_path=_Path(dep["share_path"]),
                explicit_args=dep_include_args,
                namespace_stack=dep.get("namespace_stack", []),
            )
        )

    # ── Declared args ────────────────────────────────────────────────────
    declared_arg_defaults = {a["name"]: a["default"] for a in tracked.get("declared_args", [])}

    # ── Param file deps ──────────────────────────────────────────────────
    param_file_deps = [
        _FileDependency(
            package=dep["package"],
            share_path=_Path(dep["share_path"]),
            kind=_DependencyKind.PARAM,
        )
        for dep in tracked.get("param_file_deps", [])
    ]

    # ── Per-file declared args ───────────────────────────────────────────
    declared_args_by_file: dict[tuple[str, _Path], dict[str, str]] = {}
    for key_str, args_list in tracked.get("declared_args_by_file", {}).items():
        if "://" in key_str:
            idx = key_str.index("://")
            pkg = key_str[:idx]
            sp = _Path(key_str[idx + 3 :])
        else:
            pkg = ""
            sp = _Path(key_str)
        declared_args_by_file[(pkg, sp)] = {a["name"]: a["default"] for a in args_list}

    return _ParsedLaunchFile(
        packages=list(tracked.get("packages", [])),
        nodes=resolved_nodes,
        launch_includes=launch_includes,
        param_files=param_file_deps,
        declared_arg_defaults=declared_arg_defaults,
        declared_args_by_file=declared_args_by_file,
        global_params=list(tracked.get("global_params", [])),
        warnings=list(tracked.get("warnings", [])),
        errors=list(tracked.get("errors", [])),
        infos=list(tracked.get("infos", [])) if "infos" in tracked else [],
    )


def _convert_plugins(raw_plugins: list[dict]) -> list:
    """Convert raw plugin dicts to ComposablePlugin dataclasses."""
    from launch_plus.types import (
        ComposablePlugin as _ComposablePlugin,
    )
    from launch_plus.types import (
        ParamFileInlined as _ParamFileInlined,
    )
    from launch_plus.types import (
        ParamFileReference as _ParamFileReference,
    )

    result = []
    for p in raw_plugins:
        pf_list: list[_ParamFileReference | _ParamFileInlined] = []
        for pf in p.get("param_files", []):
            if pf.get("params") is not None:
                pf_list.append(_ParamFileInlined(display=pf["path"], params=pf["params"]))
            else:
                pf_list.append(_ParamFileReference(display=pf["path"], abs=pf["path"]))
        result.append(
            _ComposablePlugin(
                package=p.get("package", ""),
                plugin=p.get("plugin", ""),
                name=p.get("name"),
                parameters=dict(p.get("parameters", {})),
                remappings=[(f, t) for f, t in p.get("remappings", [])],
                param_files=pf_list,
            )
        )
    return result


if __name__ == "__main__":
    main()
