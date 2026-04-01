"""Resolve orchestrator: combines locator, fetcher, and resolver.

Ported from crates/launch-plus-core/src/orchestrator.rs.

Key difference from Rust: the Python resolver is imported directly —
no subprocess boundary or JSON serialization. The ``run_py_resolver`` +
``py_output_to_parsed`` pipeline is replaced by a direct function call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from launch_plus.fetcher import FetchOptions
from launch_plus.locator import PackageLocator
from launch_plus.types import (
    DependencyKind,
    FileDependency,
    IncludeArgContext,
    Lockfile,
    ParsedLaunchFile,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workflow options
# ---------------------------------------------------------------------------


@dataclass
class ResolveWorkflowOptions:
    """Options controlling the resolution workflow."""

    apply_arg_defaults: bool = False
    """Apply ``default="..."`` values from ``<arg>`` elements."""

    preview: bool = False
    """Resolve from source workspace instead of AMENT_PREFIX_PATH."""

    allow_unportable_paths: bool = False
    """Allow raw filesystem paths in ``<include file=...>``."""

    rosdep_fallback: bool = False
    """Auto-install missing packages via ``rosdep``."""

    apply_opaque_file_access: bool = False
    """Allow OpaqueFunction bodies to open files via portable paths."""

    inline_params: bool = False
    """Expand ``<param from="...">`` files at resolve time."""

    show_empty_includes: bool = False
    """Show source groups even for includes that produce no resolved actions."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ResolveResult:
    """Result of recursive launch file resolution."""

    direct_packages: set[str] = field(default_factory=set)
    launch_files: list[FileDependency] = field(default_factory=list)
    param_files: list[FileDependency] = field(default_factory=list)
    other_files: list[FileDependency] = field(default_factory=list)
    fetched_packages: list[str] = field(default_factory=list)
    parsed_files: list[Path] = field(default_factory=list)
    include_args: dict[tuple[str, Path], IncludeArgContext] = field(default_factory=dict)
    declared_args_by_file: dict[tuple[str, Path], dict[str, str]] = field(default_factory=dict)
    global_params: list[list] = field(default_factory=list)
    initial_args: dict[str, str] = field(default_factory=dict)
    resolved_actions: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_package_fetched(
    lockfile: Lockfile,
    package: str,
    fetch_dir: Path,
    options: FetchOptions,
    result: ResolveResult,
    fetched_packages: set[str],
    failed_repos: set[str],
) -> bool:
    """Fetch a package if not already fetched. Returns True on success."""
    from launch_plus.fetcher import ensure_package_available

    if package in fetched_packages:
        return True

    pkg_lock = lockfile.packages.get(package)
    if pkg_lock is None:
        logger.error("package '%s' not found in lockfile", package)
        return False

    if pkg_lock.repo in failed_repos:
        return False

    pkg_path = ensure_package_available(package, lockfile, fetch_dir, options)
    if pkg_path is not None:
        fetched_packages.add(package)
        result.fetched_packages.append(package)
        return True

    logger.error("failed to fetch package '%s'", package)
    failed_repos.add(pkg_lock.repo)
    return False


# ---------------------------------------------------------------------------
# Process parsed file
# ---------------------------------------------------------------------------


def _process_parsed_file(
    parsed: ParsedLaunchFile,
    package: str,
    share_path: Path,
    file_path: Path,
    result: ResolveResult,
) -> None:
    """Common post-processing for any parsed launch file."""
    result.direct_packages.update(parsed.packages)

    # Dedup-extend param/other files
    existing_params = {(f.package, f.share_path) for f in result.param_files}
    for dep in parsed.param_files:
        if (dep.package, dep.share_path) not in existing_params:
            result.param_files.append(dep)
            existing_params.add((dep.package, dep.share_path))

    existing_other = {(f.package, f.share_path) for f in result.other_files}
    for dep in parsed.other_files:
        if (dep.package, dep.share_path) not in existing_other:
            result.other_files.append(dep)
            existing_other.add((dep.package, dep.share_path))

    result.global_params.extend(parsed.global_params)
    result.parsed_files.append(file_path)

    result.resolved_actions.extend(parsed.resolved_actions)

    # Store declared args for --show-args
    root_key = (package, share_path)
    root_args = parsed.declared_args_by_file.get(root_key, {})
    result.declared_args_by_file[root_key] = dict(root_args)
    for key, args in parsed.declared_args_by_file.items():
        result.declared_args_by_file[key] = dict(args)

    # Track launch include dependencies and populate include_args
    existing_launch = {(f.package, f.share_path) for f in result.launch_files}
    for include in parsed.launch_includes:
        key = (include.package, include.share_path)
        if key not in result.include_args:
            result.include_args[key] = IncludeArgContext(
                explicit=dict(include.explicit_args),
                namespace_stack=list(include.namespace_stack),
            )
        else:
            pass  # Same file included multiple times with different args is normal
        if key not in existing_launch:
            result.launch_files.append(
                FileDependency(
                    package=include.package,
                    share_path=include.share_path,
                    kind=DependencyKind.LAUNCH,
                )
            )
            existing_launch.add(key)


# ---------------------------------------------------------------------------
# Resolve Python launch file
# ---------------------------------------------------------------------------


def _resolve_python_file_recursive(
    lockfile: Lockfile,
    locator: PackageLocator,
    package: str,
    share_path: Path,
    fetch_dir: Path,
    options: FetchOptions,
    initial_args: dict[str, str],
    workflow_options: ResolveWorkflowOptions,
    result: ResolveResult,
    fetched_packages: set[str],
    failed_repos: set[str],
) -> None:
    """Resolve a launch file by calling py_resolver directly."""
    # Resolve file path based on mode
    file_path: Path | None = None

    if workflow_options.preview:
        if lockfile.packages.get(package) is not None:
            if not _ensure_package_fetched(
                lockfile, package, fetch_dir, options, result, fetched_packages, failed_repos
            ):
                return
            file_path = locator.resolve_share_file(package, share_path)
            if file_path is None:
                logger.error(
                    "could not locate launch file '%s' in package '%s'", share_path, package
                )
                return
        else:
            from launch_plus.fetcher import ensure_package_available

            pkg_share = ensure_package_available(
                package, None, None, rosdep_fallback=workflow_options.rosdep_fallback
            )
            if pkg_share is not None:
                file_path = pkg_share / share_path
                if not file_path.exists():
                    file_path = None
            if file_path is None:
                logger.error(
                    "package '%s' not found in lockfile or AMENT_PREFIX_PATH",
                    package,
                )
                return
    else:
        file_path = locator.resolve_install_file(package, share_path)
        if file_path is None:
            logger.error(
                "%s://%s not found in AMENT_PREFIX_PATH; "
                "run 'colcon build' first, or use --preview to resolve from source workspace",
                package,
                share_path,
            )
            return

    # Collect package shares
    package_shares = (
        locator.all_package_shares() if workflow_options.preview else locator.all_install_shares()
    )

    # Snapshot global params for this file
    current_global_params = list(result.global_params)

    # Import and call resolver directly — no subprocess!
    from launch_plus.resolver import resolve_file as _py_resolve_file

    try:
        parsed = _py_resolve_file(
            launch_file=file_path,
            args=initial_args,
            package_shares=package_shares,
            workflow_options=workflow_options,
            lockfile=lockfile,
            fetch_dir=fetch_dir,
            global_params=current_global_params,
            fetch_options=options,
        )
    except Exception as e:
        logger.error("failed to resolve launch file %s: %s", file_path, e)
        return

    _process_parsed_file(parsed, package, share_path, file_path, result)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def resolve_launch_recursive(
    lockfile: Lockfile,
    package: str,
    launcher: str,
    fetch_dir: Path,
    options: FetchOptions | None = None,
    initial_args: dict[str, str] | None = None,
    workflow_options: ResolveWorkflowOptions | None = None,
) -> ResolveResult:
    """Resolve a launch file recursively, fetching packages on demand.

    This is the main entry point for the ``launch-plus resolve`` command.
    """
    if options is None:
        options = FetchOptions()
    if initial_args is None:
        initial_args = {}
    if workflow_options is None:
        workflow_options = ResolveWorkflowOptions()

    locator = PackageLocator.from_lockfile(fetch_dir, lockfile)

    result = ResolveResult(initial_args=dict(initial_args))
    fetched_packages: set[str] = set()
    failed_repos: set[str] = set()

    # Warn if ROS_DISTRO is not set
    ros_distro = os.environ.get("ROS_DISTRO", "")
    if not ros_distro:
        logger.warning(
            "ROS_DISTRO is not set; source /opt/ros/<distro>/setup.bash for full functionality. "
            "Packages not in the lockfile will not be found via AMENT_PREFIX_PATH."
        )

    share_path = Path("launch") / launcher
    _resolve_python_file_recursive(
        lockfile,
        locator,
        package,
        share_path,
        fetch_dir,
        options,
        initial_args,
        workflow_options,
        result,
        fetched_packages,
        failed_repos,
    )

    logger.info(
        "Resolution complete: %d direct packages, %d packages fetched",
        len(result.direct_packages),
        len(result.fetched_packages),
    )

    return result
