"""Package and launch file locator.

Two resolution modes:
1. **Lockfile mode** (``resolve_*``): Deterministic paths from lockfile, no disk check.
2. **Discovery mode** (``locate_*``): Filesystem search + AMENT_PREFIX_PATH fallback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from roscope.types import Lockfile, PackageLock

logger = logging.getLogger(__name__)


class MultipleLaunchFilesError(LookupError):
    """Raised when a file name matches more than one file in a share directory."""

    def __init__(self, msg: str, paths: list[Path]) -> None:
        super().__init__(msg)
        self.paths = paths


def find_share_file(share_dir: Path, file_name: str) -> Path:
    """Search every directory under ``share_dir`` for a file named exactly ``file_name``.

    Mirrors upstream ``ros2launch``'s ``get_share_file_path_from_package``.

    :raises FileNotFoundError: if no file named ``file_name`` exists under ``share_dir``.
    :raises MultipleLaunchFilesError: if more than one file matches.
    """
    matches = [
        Path(root) / name
        for root, _dirs, files in os.walk(share_dir)
        for name in files
        if name == file_name
    ]
    if not matches:
        raise FileNotFoundError(
            f"file '{file_name}' was not found in the share directory '{share_dir}'"
        )
    if len(matches) > 1:
        raise MultipleLaunchFilesError(
            f"file '{file_name}' was found more than once in the share directory "
            f"'{share_dir}': {[str(p) for p in matches]}",
            matches,
        )
    return matches[0]


class PackageLocator:
    """Resolves package names to file system paths."""

    def __init__(
        self,
        workspace_src: Path | None = None,
        lockfile: Lockfile | None = None,
        ament_prefixes: list[Path] | None = None,
    ) -> None:
        self.workspace_src = workspace_src.resolve() if workspace_src else None
        self.lockfile = lockfile
        self.ament_prefixes: list[Path] = list(ament_prefixes) if ament_prefixes else []

    # -- constructors --------------------------------------------------------

    @classmethod
    def with_workspace(cls, workspace_src: Path | str) -> PackageLocator:
        return cls(workspace_src=Path(workspace_src))

    @classmethod
    def from_lockfile(cls, workspace_src: Path | str, lockfile: Lockfile) -> PackageLocator:
        loc = cls(workspace_src=Path(workspace_src), lockfile=lockfile)
        loc.add_ament_from_env()
        return loc

    @classmethod
    def from_workspace(cls, workspace_src: Path | str) -> PackageLocator:
        loc = cls(workspace_src=Path(workspace_src))
        loc.add_ament_from_env()
        return loc

    # -- mutators ------------------------------------------------------------

    def add_ament_from_env(self) -> PackageLocator:
        ament_path = os.environ.get("AMENT_PREFIX_PATH", "")
        for prefix in ament_path.split(":"):
            if prefix:
                self.ament_prefixes.append(Path(prefix))
        return self

    def add_ament_prefix(self, prefix: Path | str) -> PackageLocator:
        self.ament_prefixes.append(Path(prefix))
        return self

    # ========================================================================
    # Lockfile mode: deterministic path resolution (no existence check)
    # ========================================================================

    def get_package_lock(self, package: str) -> PackageLock | None:
        if self.lockfile is None:
            return None
        return self.lockfile.packages.get(package)

    def has_package(self, package: str) -> bool:
        return self.get_package_lock(package) is not None

    def resolve_package_share(self, package: str) -> Path | None:
        """Compute expected package share path from lockfile (no disk check)."""
        if self.lockfile is None or self.workspace_src is None:
            return None
        pkg_lock = self.lockfile.packages.get(package)
        if pkg_lock is None:
            return None
        return self.workspace_src / pkg_lock.repo / pkg_lock.path

    def resolve_launch_file(self, package: str, launcher: str) -> Path | None:
        """Compute ``<workspace>/<repo>/<pkg_path>/launch/<launcher>``."""
        pkg_share = self.resolve_package_share(package)
        if pkg_share is None:
            return None
        return pkg_share / "launch" / launcher

    def resolve_share_file(self, package: str, share_path: Path | str) -> Path | None:
        """Compute ``<workspace>/<repo>/<pkg_path>/<share_path>``."""
        pkg_share = self.resolve_package_share(package)
        if pkg_share is None:
            return None
        return pkg_share / share_path

    def resolve_install_file(self, package: str, share_path: Path | str) -> Path | None:
        """Find ``<ament_prefix>/share/<package>/<share_path>`` on disk."""
        for prefix in self.ament_prefixes:
            path = prefix / "share" / package / share_path
            if path.exists():
                return path
        return None

    def locate_install_share(self, package: str) -> Path | None:
        """Find ``<ament_prefix>/share/<package>`` on disk."""
        for prefix in self.ament_prefixes:
            share_path = prefix / "share" / package
            if share_path.exists():
                return share_path
        return None

    def resolve_launch_file_or_err(self, package: str, launcher: str) -> Path:
        """Like :meth:`resolve_launch_file` but raises on failure."""
        result = self.resolve_launch_file(package, launcher)
        if result is not None:
            return result
        if self.lockfile is None:
            raise LookupError("no lockfile configured in locator")
        if self.workspace_src is None:
            raise LookupError("no workspace configured in locator")
        raise LookupError(f"package '{package}' not found in lockfile")

    # ========================================================================
    # Discovery mode: filesystem search (checks existence)
    # ========================================================================

    def locate_package_share(self, package: str) -> Path | None:
        """Search filesystem for an existing package directory.

        Search order:
        1. Lockfile + workspace (if configured and exists on disk)
        2. Direct workspace scan (common layouts)
        3. AMENT_PREFIX_PATH (installed packages)
        """
        # Strategy 1: lockfile + workspace
        if self.lockfile is not None and self.workspace_src is not None:
            pkg_lock = self.lockfile.packages.get(package)
            if pkg_lock is not None:
                pkg_path = self.workspace_src / pkg_lock.repo / pkg_lock.path
                if pkg_path.exists():
                    return pkg_path

        # Strategy 2: direct workspace scan
        if self.workspace_src is not None:
            ws = self.workspace_src
            # Python package layout: <pkg>/<pkg>
            python_layout = ws / package / package
            if python_layout.exists():
                return python_layout
            # Simple layout: <pkg> with package.xml
            simple_layout = ws / package
            if simple_layout.exists() and (simple_layout / "package.xml").exists():
                return simple_layout
            # Recursive search
            found = self._search_package_in_workspace(ws, package)
            if found is not None:
                return found

        # Strategy 3: AMENT_PREFIX_PATH
        for prefix in self.ament_prefixes:
            share_path = prefix / "share" / package
            if share_path.exists():
                return share_path

        return None

    def _search_package_in_workspace(self, workspace: Path, package: str) -> Path | None:
        """Recursively search for a package directory in the workspace."""
        try:
            entries = list(workspace.iterdir())
        except OSError:
            logger.debug("cannot read workspace dir %s", workspace)
            return None

        for entry in entries:
            if not entry.is_dir():
                continue
            # <repo>/<package> with package.xml
            nested = entry / package
            if nested.exists() and (nested / "package.xml").exists():
                return nested
            # Python layout: <repo>/<package>/<package>
            python_nested = nested / package
            if python_nested.exists():
                return python_nested
            # One level deeper (monorepos)
            try:
                sub_entries = list(entry.iterdir())
            except OSError:
                continue
            for sub_entry in sub_entries:
                if sub_entry.is_dir():
                    deep_nested = sub_entry / package
                    if deep_nested.exists() and (deep_nested / "package.xml").exists():
                        return deep_nested
        return None

    def locate_launch_file(self, package: str, launcher: str) -> Path | None:
        """Search the package's share directory for a file named ``launcher``.

        Returns ``None`` if the package itself cannot be located.

        :raises MultipleLaunchFilesError: if ``launcher`` matches more than one file.
        """
        pkg_share = self.locate_package_share(package)
        if pkg_share is None:
            return None
        try:
            return find_share_file(pkg_share, launcher)
        except FileNotFoundError:
            return None

    def locate_launch_file_or_err(self, package: str, launcher: str) -> Path:
        """Like :meth:`locate_launch_file` but raises on failure."""
        result = self.locate_launch_file(package, launcher)
        if result is not None:
            return result
        raise FileNotFoundError(f"launch file '{launcher}' not found in package '{package}'")

    # ========================================================================
    # Bulk lookups
    # ========================================================================

    def all_package_shares(self) -> dict[str, str]:
        """All known package share paths (workspace source + AMENT_PREFIX_PATH).

        Used in **preview mode only** — source paths are listed first so the
        resolver finds them before the (possibly absent) install paths.
        In post-build mode use :meth:`all_install_shares` instead so that only
        the installed workspace is consulted.
        """
        result: dict[str, str] = {}
        if self.lockfile is not None and self.workspace_src is not None:
            for pkg_name, pkg_lock in self.lockfile.packages.items():
                share_path = self.workspace_src / pkg_lock.repo / pkg_lock.path
                result[pkg_name] = str(share_path)
        for prefix in self.ament_prefixes:
            share_root = prefix / "share"
            if share_root.is_dir():
                for entry in share_root.iterdir():
                    if entry.is_dir():
                        result.setdefault(entry.name, str(entry))
        return result

    def all_install_shares(self) -> dict[str, str]:
        """Package share paths from AMENT_PREFIX_PATH only.

        Used in non-preview mode for installed artifacts.
        """
        result: dict[str, str] = {}
        for prefix in self.ament_prefixes:
            share_root = prefix / "share"
            if share_root.is_dir():
                for entry in share_root.iterdir():
                    if entry.is_dir():
                        result.setdefault(entry.name, str(entry))
        return result
