"""IR (Intermediate Representation) types for roscope.

These dataclasses represent the resolved launch graph — the output of the
resolver and the input to the renderer.  They are the shared vocabulary between
all roscope modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DependencyKind(Enum):
    """Kind of file dependency discovered during resolution."""

    LAUNCH = auto()
    """Launch file — needs recursive parsing to discover more dependencies."""
    PARAM = auto()
    """Parameter/config file — just needs fetching, no parsing."""
    OTHER = auto()
    """Other file type — needs fetching, no special handling."""


# ---------------------------------------------------------------------------
# Dataclasses — small building blocks
# ---------------------------------------------------------------------------


@dataclass
class FileDependency:
    """A file dependency discovered during resolution."""

    package: str
    """Package name."""
    share_path: Path
    """Path relative to package share directory."""
    kind: DependencyKind


@dataclass
class IncludeArgContext:
    """Argument context at an include boundary."""

    explicit: dict[str, str] = field(default_factory=dict)
    """Args explicitly forwarded via ``<arg name="..." value="..."/>``."""
    with_cascade: dict[str, str] = field(default_factory=dict)
    """Full parent arg context + explicit overrides (for ``--allow-global-arg-cascade``)."""
    namespace_stack: list[str] = field(default_factory=list)
    """Accumulated ``<push-ros-namespace>`` stack at the include site."""


@dataclass
class LaunchInclude:
    """An included launch file, ready for recursive processing by the orchestrator."""

    package: str
    share_path: Path
    explicit_args: dict[str, str] = field(default_factory=dict)
    namespace_stack: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ParsedLaunchFile — output of the resolver for one file
# ---------------------------------------------------------------------------


@dataclass
class ParsedLaunchFile:
    """Result of resolving a single launch file."""

    packages: list[str] = field(default_factory=list)
    launch_includes: list[LaunchInclude] = field(default_factory=list)
    param_files: list[FileDependency] = field(default_factory=list)
    other_files: list[FileDependency] = field(default_factory=list)
    global_params: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lockfile types (ported from indexer.rs)
# ---------------------------------------------------------------------------


@dataclass
class RepoLock:
    """A pinned repository in the lockfile."""

    url: str
    """Git repository URL."""
    version: str
    """Pinned SHA."""
    repo_type: str = "git"
    """Repository type (always ``"git"``)."""
    version_ref: str | None = None
    """Original version reference (tag, branch, or short SHA)."""
    packages: list[str] = field(default_factory=list)
    """Package names contained in this repository."""


@dataclass
class PackageLock:
    """A pinned package in the lockfile."""

    repo: str
    """Key into :attr:`Lockfile.repositories`."""
    path: str
    """Path within the repository."""


@dataclass
class Lockfile:
    """Dual-indexed lockfile: repositories + packages."""

    repositories: dict[str, RepoLock] = field(default_factory=dict)
    """Repository-centric view (for fetching)."""
    packages: dict[str, PackageLock] = field(default_factory=dict)
    """Package-centric view (for O(1) lookup)."""

    def remove_repo(self, workspace_path: str) -> None:
        """Remove a repository and all its packages from the lockfile."""
        repo = self.repositories.pop(workspace_path, None)
        if repo is not None:
            for pkg_name in repo.packages:
                self.packages.pop(pkg_name, None)


# ---------------------------------------------------------------------------
# .repos file types
# ---------------------------------------------------------------------------


@dataclass
class RepoEntry:
    """A single repository entry in a .repos file."""

    repo_type: str
    """Repository type (always ``"git"``)."""
    url: str
    """Git repository URL."""
    version: str
    """Version specification (tag, branch, or SHA)."""


@dataclass
class ReposFile:
    """A .repos file in VCS format."""

    repositories: dict[str, RepoEntry] = field(default_factory=dict)
    """Map of repository path to repository entry."""


# ---------------------------------------------------------------------------
# Package dependency types (from package.xml)
# ---------------------------------------------------------------------------


@dataclass
class Dependencies:
    """Package dependencies categorized by REP-149 tag type.

    Parsed from on-disk ``package.xml`` at resolve/build time — NOT stored in lockfile.

    ``<depend>`` expands to ``build + build_export + exec`` (REP-149 §3).
    """

    build: list[str] = field(default_factory=list)
    """``<build_depend>``: needed to compile this package."""
    build_export: list[str] = field(default_factory=list)
    """``<build_export_depend>``: needed by packages that compile against this one."""
    buildtool: list[str] = field(default_factory=list)
    """``<buildtool_depend>``: build tool needed to build this package."""
    buildtool_export: list[str] = field(default_factory=list)
    """``<buildtool_export_depend>``: build tool exported by this package."""
    exec: list[str] = field(default_factory=list)
    """``<exec_depend>``: needed at runtime."""
    test: list[str] = field(default_factory=list)
    """``<test_depend>``: needed only for testing."""


@dataclass
class PackageInfo:
    """Information about a package found in a repository."""

    name: str
    """Package name from package.xml."""
    path: str
    """Path within the repository (directory containing package.xml)."""
    dependencies: Dependencies = field(default_factory=Dependencies)
    """Dependencies extracted from package.xml."""


class DependencyMode(Enum):
    """Dependency resolution mode."""

    BUILD = auto()
    """Build dependencies only (for colcon build)."""
    EXEC = auto()
    """Execution dependencies only (for runtime)."""
    BUILD_AND_EXEC = auto()
    """Both build and exec dependencies."""
    ALL = auto()
    """All dependencies including test."""


@dataclass
class DependencyGraph:
    """Result of transitive dependency resolution."""

    packages: set[str] = field(default_factory=set)
    """All packages needed (root + transitive)."""
    missing: set[str] = field(default_factory=set)
    """Packages that were requested but not found in lockfile."""
    external: set[str] = field(default_factory=set)
    """Packages that are external (not in lockfile, e.g., system deps)."""


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------


def ros2_namespace_join(base: str | None, next_ns: str) -> str | None:
    """Join a base namespace with a next component, following ROS 2 semantics.

    - Relative components are appended.
    - Absolute components reset the accumulated base.
    """
    next_ns = next_ns.rstrip("/")
    if not next_ns:
        return base
    if next_ns.startswith("/"):
        return next_ns
    if base is None or base in ("", "/"):
        return f"/{next_ns}"
    return f"{base.rstrip('/')}/{next_ns}"


def effective_namespace(stack: list[str], node_ns: str | None = None) -> str | None:
    """Compute effective ROS 2 namespace from push-ros-namespace stack + node namespace.

    Follows ROS 2 ``namespace_join`` semantics applied left-to-right:

    - Relative components are appended (e.g. ``["sensing", "lidar"]`` -> ``"/sensing/lidar"``).
    - Absolute components reset the accumulated base (e.g. ``["sensing", "/abs"]`` -> ``"/abs"``).
    - The node's explicit ``namespace=`` attribute is applied last with the same rules.
    - Returns ``None`` when both the stack and the explicit namespace are empty.
    """
    current: str | None = None
    for component in stack:
        current = ros2_namespace_join(current, component)
    if node_ns is not None:
        current = ros2_namespace_join(current, node_ns)
    return current
