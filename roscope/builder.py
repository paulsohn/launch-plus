"""Builder: compute a selective colcon build plan from a resolved launch target.

Ported from crates/roscope-core/src/builder.rs.

The build plan is derived from the launch resolver output:
1. ``direct_packages`` — packages directly referenced in the launch graph.
2. ``resolve_dependencies`` with ``BuildAndExec`` mode — transitive expansion.
3. ``compute_build_order`` — Kahn's topological sort.
4. ``colcon build --packages-select <ordered list>``
"""

from __future__ import annotations

import logging
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from roscope.exceptions import ProcessExecutionError, RoscopeError
from roscope.fetcher import FetchOptions, fetch_packages
from roscope.indexer import compute_build_order, resolve_dependencies
from roscope.types import DependencyMode, Lockfile

logger = logging.getLogger(__name__)


@dataclass
class BuildPlan:
    """Everything colcon needs to build the launch target."""

    packages: list[str] = field(default_factory=list)
    """Lockfile packages to build, in topological order (dependencies first)."""

    external_deps: set[str] = field(default_factory=set)
    """External dependencies not in the lockfile (e.g. system ROS packages)."""

    src_dir: Path = field(default_factory=Path)
    """Source directory passed to ``colcon build --base-paths``."""

    build_base: Path = field(default_factory=Path)
    """Colcon build output directory (``--build-base``)."""

    install_base: Path = field(default_factory=Path)
    """Colcon install prefix (``--install-base``)."""

    log_base: Path = field(default_factory=Path)
    """Colcon log directory (``--log-base``)."""


@dataclass
class BuildOptions:
    """Options controlling how the build is executed."""

    dry_run: bool = False
    """Print the colcon command without running it."""

    extra_colcon_args: list[str] = field(default_factory=list)
    """Extra arguments inserted into ``colcon build`` before ``--packages-select``."""


def plan_build_from_packages(
    seed_packages: set[str],
    lockfile: Lockfile,
    src_dir: Path,
    build_base: Path,
    install_base: Path,
    log_base: Path,
    fetch_options: FetchOptions | None = None,
    test_mode: bool = False,
) -> BuildPlan:
    """Compute a build plan from explicit seed packages, fetching transitive deps as needed.

    Expands transitively by reading on-disk ``package.xml``. Packages whose
    ``package.xml`` is not yet on disk are fetched, and the expansion is retried
    until the graph stabilises (up to 10 rounds).
    """
    if fetch_options is None:
        fetch_options = FetchOptions()

    mode = DependencyMode.ALL if test_mode else DependencyMode.BUILD_AND_EXEC

    # Iterative expand+fetch
    graph = resolve_dependencies(lockfile, src_dir, seed_packages, mode)
    for _ in range(10):
        if not graph.missing:
            break

        missing = sorted(graph.missing)
        logger.info(
            "Fetching %d transitive build deps not yet on disk: %s",
            len(missing),
            missing,
        )
        try:
            fetched = fetch_packages(missing, lockfile, src_dir, fetch_options)
        except RoscopeError:
            break
        if not fetched:
            break

        graph = resolve_dependencies(lockfile, src_dir, seed_packages, mode)

    packages = compute_build_order(lockfile, src_dir, graph.packages)

    return BuildPlan(
        packages=packages,
        external_deps=graph.external,
        src_dir=src_dir,
        build_base=build_base,
        install_base=install_base,
        log_base=log_base,
    )


def execute_build(plan: BuildPlan, options: BuildOptions | None = None) -> None:
    """Execute ``colcon build --packages-select <packages>``.

    When ``options.dry_run`` is True, prints the command without running it.
    """
    if options is None:
        options = BuildOptions()

    if not plan.packages:
        logger.info("No packages to build.")
        return

    # --log-base is a colcon global argument (before the verb)
    args: list[str] = [
        "--log-base",
        str(plan.log_base),
        "build",
        "--base-paths",
        str(plan.src_dir),
        "--build-base",
        str(plan.build_base),
        "--install-base",
        str(plan.install_base),
    ]

    args.extend(options.extra_colcon_args)
    args.append("--packages-select")
    args.extend(plan.packages)

    cmd_str = f"colcon {' '.join(args)}"

    if options.dry_run:
        print(cmd_str)  # noqa: T201
        return

    logger.info("%s", cmd_str)

    # Spawn colcon and forward SIGINT
    interrupted = False
    prev_handler = signal.getsignal(signal.SIGINT)

    def _sigint_handler(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
        proc = subprocess.run(["colcon", *args], check=False)
    finally:
        signal.signal(signal.SIGINT, prev_handler)

    if interrupted:
        raise ProcessExecutionError("build interrupted by Ctrl+C")

    if proc.returncode != 0:
        raise ProcessExecutionError(f"colcon build failed with status: {proc.returncode}")
