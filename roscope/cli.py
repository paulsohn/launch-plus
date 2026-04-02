"""CLI for roscope — Bazel-like build and run system for ROS 2."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from roscope.exceptions import RoscopeError

if TYPE_CHECKING:
    from roscope.fetcher import WorkspaceState
    from roscope.orchestrator import ResolveWorkflowOptions
    from roscope.types import Lockfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    """Configure logging to stderr."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _parse_launch_args(args: tuple[str, ...]) -> dict[str, str]:
    """Parse ``name:=value`` launch argument strings into a dict."""
    result: dict[str, str] = {}
    for arg in args:
        if ":=" not in arg:
            raise click.BadParameter(
                f"invalid launch argument '{arg}': expected 'name:=value' format"
            )
        name, value = arg.split(":=", 1)
        result[name] = value
    return result


def _parse_workspace_state(clean: bool, dirty: bool) -> WorkspaceState:
    """Map --clean/--dirty flags to WorkspaceState."""
    from roscope.fetcher import WorkspaceState

    if clean:
        return WorkspaceState.CLEAN
    if dirty:
        return WorkspaceState.DIRTY
    return WorkspaceState.DEFAULT


def _read_colcon_flagfile(path: str) -> list[str]:
    """Read a colcon flagfile and return the tokens.

    Format: one token per line; # comments and blank lines are ignored.
    """
    content = Path(path).read_text()
    tokens: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Reject flags managed by roscope
        if line.startswith("--packages-") or line in (
            "--base-paths",
            "--build-base",
            "--install-base",
            "--log-base",
        ):
            raise click.ClickException(
                f"colcon flagfile '{path}': '{line}' conflicts with a roscope-managed "
                "argument; remove it from the flagfile"
            )
        tokens.append(line)
    return tokens


def _read_lockfile(lockfile_path: str) -> Lockfile:
    """Read and parse a lockfile."""
    from roscope.indexer import parse_lockfile

    p = Path(lockfile_path)
    if not p.exists():
        raise click.ClickException(
            f"lockfile not found: {p.resolve()}\n"
            f"  Run from a directory containing {p.name}, or pass --lockfile <path>."
        )
    content = p.read_text()
    return parse_lockfile(content)


def _contains_git_dir(path: Path) -> bool:
    """Check if a directory contains a .git directory (recursively)."""
    if (path / ".git").exists():
        return True
    try:
        for entry in path.iterdir():
            if entry.is_dir() and _contains_git_dir(entry):
                return True
    except OSError:
        pass
    return False


def _clean_directory_keep_git(path: Path) -> None:
    """Clean directory contents while preserving .git directories."""
    for entry in path.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            if _contains_git_dir(entry):
                _clean_directory_keep_git(entry)
            else:
                shutil.rmtree(entry)
        else:
            entry.unlink()


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option()
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Bazel-like build and run system for ROS 2."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


@main.command()
@click.argument("files", nargs=-1)
@click.option("--append", is_flag=True, help="Append to existing lockfile")
@click.option("-o", "--output", "--lockfile", default=None, help="Output lockfile path")
@click.option("--src", default="src", help="Source directory for cloning repositories")
@click.option("--no-recurse-submodules", is_flag=True, help="Disable submodule recursion")
@click.option("--verify", is_flag=True, help="Verify lockfile consistency")
@click.pass_context
def index(
    ctx: click.Context,
    files: tuple[str, ...],
    append: bool,
    output: str | None,
    src: str,
    no_recurse_submodules: bool,
    verify: bool,
) -> None:
    """Generate lockfile from .repos files."""
    from roscope.indexer import (
        generate_lockfile,
        parse_lockfile,
        parse_repos,
        serialize_lockfile,
    )
    from roscope.types import Lockfile

    if not files:
        files = ("manifest.repos",)
    output_path = output or "manifest.lock.repos"

    if verify:
        exit_code = _cmd_verify(files, output_path)
        sys.exit(exit_code)

    src_path = Path(src)
    logger.info("Using source directory: %s", src_path)

    # Start with existing lockfile if appending
    if append and Path(output_path).exists():
        content = Path(output_path).read_text()
        lockfile = parse_lockfile(content)
    else:
        lockfile = Lockfile()

    # Process each .repos file
    for file in files:
        logger.info("Processing %s", file)
        content = Path(file).read_text()
        repos = parse_repos(content)
        logger.info("Found %d repositories in %s", len(repos.repositories), file)

        # Atomic update: remove old entries
        for workspace_path in repos.repositories:
            if workspace_path in lockfile.repositories:
                logger.debug("Removing old entry for %s before re-indexing", workspace_path)
                lockfile.remove_repo(workspace_path)

        file_lockfile = generate_lockfile(
            repos, src_dir=src_path, recurse_submodules=not no_recurse_submodules
        )

        # Merge
        lockfile.repositories.update(file_lockfile.repositories)
        for name, pkg in file_lockfile.packages.items():
            if name in lockfile.packages:
                existing = lockfile.packages[name]
                if existing.repo not in repos.repositories:
                    logger.warning(
                        "Package %s already exists in %s, skipping from %s",
                        name,
                        existing.repo,
                        pkg.repo,
                    )
                    continue
            lockfile.packages[name] = pkg

    yaml_str = serialize_lockfile(lockfile)
    Path(output_path).write_text(yaml_str)

    logger.info(
        "Wrote lockfile with %d repositories and %d packages to %s",
        len(lockfile.repositories),
        len(lockfile.packages),
        output_path,
    )
    click.echo(
        f"Generated {output_path} with {len(lockfile.repositories)} repositories "
        f"and {len(lockfile.packages)} packages"
    )


def _cmd_verify(files: tuple[str, ...], lockfile_path: str) -> int:
    """Verify lockfile consistency. Returns exit code."""
    from roscope.indexer import parse_lockfile, parse_repos

    if not Path(lockfile_path).exists():
        click.echo(f"Error: Lockfile not found: {lockfile_path}")
        click.echo("Run 'roscope index' to generate it.")
        return 2

    content = Path(lockfile_path).read_text()
    lockfile = parse_lockfile(content)

    click.echo(f"Verifying against {lockfile_path}...")

    all_ok = True
    missing = False
    total_repos = 0

    for file in files:
        file_content = Path(file).read_text()
        repos = parse_repos(file_content)

        for workspace_path, entry in repos.repositories.items():
            total_repos += 1
            lock_entry = lockfile.repositories.get(workspace_path)
            if lock_entry is None:
                click.echo(f"  {workspace_path}: MISSING from lockfile")
                missing = True
                continue

            is_sha = len(entry.version) == 40 and all(
                c in "0123456789abcdefABCDEF" for c in entry.version
            )

            if is_sha:
                expected, actual, label = entry.version, lock_entry.version, "sha"
            else:
                actual = lock_entry.version_ref or lock_entry.version
                expected, label = entry.version, "ref"

            if expected == actual:
                click.echo(f"  {workspace_path}: OK ({label}={expected})")
            else:
                click.echo(f"  {workspace_path}: MISMATCH")
                click.echo(f"    .repos {label}: {expected}")
                click.echo(f"    lockfile {label}: {actual}")
                all_ok = False

    click.echo()

    if missing:
        click.echo(f"Error: {', '.join(files)} has repositories not in lockfile.")
        click.echo("Run 'roscope index' to regenerate.")
        return 2

    if not all_ok:
        click.echo("Error: Lockfile inconsistent with .repos files.")
        click.echo("Run 'roscope index' to regenerate.")
        return 1

    click.echo(f"All {total_repos} repositories consistent.")
    return 0


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@main.command()
@click.argument("repos", nargs=-1)
@click.option("-i", "--input", "--lockfile", "input_path", default="manifest.lock.repos")
@click.option("-o", "--output", "--output-lockfile", "output_path", default=None)
@click.option("--src", default="src", help="Source directory for local clones")
@click.option("--diff", is_flag=True, help="Show what would change without updating")
@click.pass_context
def update(
    ctx: click.Context,
    repos: tuple[str, ...],
    input_path: str,
    output_path: str | None,
    src: str,
    diff: bool,
) -> None:
    """Update lockfile with latest SHAs by re-resolving refs."""
    from roscope.indexer import (
        blobless_clone,
        discover_packages,
        parse_lockfile,
        resolve_version_local,
        serialize_lockfile,
    )
    from roscope.types import PackageLock

    output_file = output_path or input_path
    src_path = Path(src)

    content = Path(input_path).read_text()
    lockfile = parse_lockfile(content)

    logger.info("Loaded lockfile with %d repositories", len(lockfile.repositories))
    if repos:
        logger.info("Updating only: %s", repos)

    updates: list[tuple[str, str, str]] = []  # (ws_path, old_sha, new_sha)
    errors: list[tuple[str, str]] = []

    for workspace_path, repo in lockfile.repositories.items():
        if repos and workspace_path not in repos:
            continue
        if repo.version_ref is None:
            logger.debug("Skipping %s (pinned to SHA, no ref)", workspace_path)
            continue

        logger.debug(
            "Checking %s (ref=%s, current=%s)",
            workspace_path,
            repo.version_ref,
            repo.version[:8],
        )

        repo_dir = src_path / workspace_path
        try:
            if not (repo_dir / ".git").exists():
                logger.debug("No local clone for %s, blobless-cloning", workspace_path)
                blobless_clone(repo.url, repo_dir)
            new_sha = resolve_version_local(repo_dir, repo.url, repo.version_ref)
            if new_sha != repo.version:
                updates.append((workspace_path, repo.version, new_sha))
        except RoscopeError as e:
            errors.append((workspace_path, str(e)))

    for workspace_path, error in errors:
        logger.warning("Failed to resolve %s: %s", workspace_path, error)

    if not updates:
        click.echo("All repositories are up to date.")
        return

    click.echo(f"Found {len(updates)} updates:")
    for workspace_path, old_sha, new_sha in updates:
        click.echo(f"  {workspace_path} {old_sha[:8]} -> {new_sha[:8]}")

    if diff:
        click.echo("\nRun without --diff to apply updates.")
        return

    click.echo("\nApplying updates...")
    for workspace_path, _old_sha, new_sha in updates:
        repo = lockfile.repositories[workspace_path]
        old_packages = list(repo.packages)
        repo.version = new_sha

        repo_dir = src_path / workspace_path
        if repo_dir.exists():
            logger.debug("Re-scanning packages in %s", workspace_path)
            try:
                packages = discover_packages(repo.url, new_sha, repo_dir, True)
                for pkg_name in old_packages:
                    lockfile.packages.pop(pkg_name, None)
                repo.packages = sorted(p.name for p in packages)
                for pkg in packages:
                    lockfile.packages[pkg.name] = PackageLock(repo=workspace_path, path=pkg.path)
            except RoscopeError as e:
                logger.warning(
                    "Failed to re-scan packages in %s: %s. Keeping old package list.",
                    workspace_path,
                    e,
                )

    yaml_str = serialize_lockfile(lockfile)
    Path(output_file).write_text(yaml_str)

    click.echo(
        f"Updated {output_file} with {len(lockfile.repositories)} repositories "
        f"and {len(lockfile.packages)} packages"
    )


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


@main.command()
@click.argument("packages", nargs=-1, required=True)
@click.option("-l", "--lockfile", default="manifest.lock.repos", help="Lockfile path")
@click.option("--src", default="src", help="Fetch directory")
@click.option("--no-recurse-submodules", is_flag=True)
@click.option("--shallow", is_flag=True, help="Use shallow clone (depth=1)")
@click.pass_context
def fetch(
    ctx: click.Context,
    packages: tuple[str, ...],
    lockfile: str,
    src: str,
    no_recurse_submodules: bool,
    shallow: bool,
) -> None:
    """Fetch packages from lockfile using sparse-checkout."""
    from roscope.fetcher import FetchOptions, WorkspaceState, fetch_packages

    parsed_lockfile = _read_lockfile(lockfile)
    fetch_path = Path(src)
    options = FetchOptions(
        recurse_submodules=not no_recurse_submodules,
        shallow=shallow,
        workspace_state=WorkspaceState.CLEAN,
    )

    logger.info("Fetching %d packages into %s", len(packages), src)
    fetched = fetch_packages(list(packages), parsed_lockfile, fetch_path, options)

    click.echo(f"Fetched {len(fetched)} packages:")
    for pkg in fetched:
        click.echo(f"  {pkg.name} -> {pkg.path}")


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.argument("args", nargs=-1)
@click.option("-l", "--lockfile", default="manifest.lock.repos", help="Lockfile path")
@click.option("--src", default="src", help="Source directory")
@click.option("--report", is_flag=True, help="Print dependency report to stderr")
@click.option("--preview", is_flag=True, help="Resolve from source workspace")
@click.option("--show-args", is_flag=True)
@click.option("--inline-params", is_flag=True)
@click.option("--rosdep", is_flag=True, help="Install missing packages via rosdep")
@click.option(
    "--show-empty-includes",
    is_flag=True,
    help="Show source groups for includes with no resolved actions",
)
@click.option("-c", "--clean", is_flag=True)
@click.option("-d", "--dirty", is_flag=True)
@click.option("--shallow", is_flag=True)
@click.option("--visualize", is_flag=True, help="Open graph visualizer in browser")
@click.pass_context
def resolve(
    ctx: click.Context,
    package: str,
    launcher: str,
    args: tuple[str, ...],
    lockfile: str,
    src: str,
    report: bool,
    preview: bool,
    show_args: bool,
    inline_params: bool,
    rosdep: bool,
    show_empty_includes: bool,
    clean: bool,
    dirty: bool,
    shallow: bool,
    visualize: bool,
) -> None:
    """Resolve launch file (no build)."""
    from roscope.orchestrator import ResolveWorkflowOptions

    initial_args = _parse_launch_args(args)
    workspace_state = _parse_workspace_state(clean, dirty)

    workflow_options = ResolveWorkflowOptions(
        preview=preview,
        rosdep_fallback=rosdep,
        inline_params=inline_params,
        show_empty_includes=show_empty_includes,
        show_args=show_args,
    )

    _cmd_resolve(
        package=package,
        launcher=launcher,
        initial_args=initial_args,
        lockfile_path=lockfile,
        src_dir=src,
        report=report,
        preview=preview,
        show_args=show_args,
        suppress_xml=False,
        strict=False,
        workspace_state=workspace_state,
        workflow_options=workflow_options,
        shallow=shallow,
        visualize=visualize,
    )


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.argument("args", nargs=-1)
@click.option("-l", "--lockfile", default="manifest.lock.repos", help="Lockfile path")
@click.option("--src", default="src", help="Source directory")
@click.option("--preview", is_flag=True)
@click.option("--rosdep", is_flag=True)
@click.option("--strict", is_flag=True, help="Treat warnings as errors")
@click.option("-c", "--clean", is_flag=True)
@click.option("-d", "--dirty", is_flag=True)
@click.option("--shallow", is_flag=True)
@click.pass_context
def check(
    ctx: click.Context,
    package: str,
    launcher: str,
    args: tuple[str, ...],
    lockfile: str,
    src: str,
    preview: bool,
    rosdep: bool,
    strict: bool,
    clean: bool,
    dirty: bool,
    shallow: bool,
) -> None:
    """Validate launch file without running."""
    from roscope.orchestrator import ResolveWorkflowOptions

    initial_args = _parse_launch_args(args)
    workspace_state = _parse_workspace_state(clean, dirty)

    workflow_options = ResolveWorkflowOptions(
        preview=preview,
        rosdep_fallback=rosdep,
        inline_params=False,
    )

    _cmd_resolve(
        package=package,
        launcher=launcher,
        initial_args=initial_args,
        lockfile_path=lockfile,
        src_dir=src,
        report=False,
        preview=preview,
        show_args=False,
        suppress_xml=True,
        strict=strict,
        workspace_state=workspace_state,
        workflow_options=workflow_options,
        shallow=shallow,
    )


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.argument("args", nargs=-1)
@click.option("-l", "--lockfile", default="manifest.lock.repos", help="Lockfile path")
@click.option("--src", default="src", help="Source directory")
@click.option("-c", "--clean", is_flag=True)
@click.option("-d", "--dirty", is_flag=True)
@click.option("--shallow", is_flag=True)
@click.option("--rosdep", is_flag=True)
@click.option("--build-base", default="build")
@click.option("--install-base", default="install")
@click.option("--log-base", default="log")
@click.option("--colcon-flagfile", default=None, type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Print colcon command without running it")
@click.pass_context
def build(
    ctx: click.Context,
    package: str,
    launcher: str,
    args: tuple[str, ...],
    lockfile: str,
    src: str,
    clean: bool,
    dirty: bool,
    shallow: bool,
    rosdep: bool,
    build_base: str,
    install_base: str,
    log_base: str,
    colcon_flagfile: str | None,
    dry_run: bool,
) -> None:
    """Fetch and build packages for a launcher."""
    from roscope.orchestrator import ResolveWorkflowOptions

    initial_args = _parse_launch_args(args)
    workspace_state = _parse_workspace_state(clean, dirty)

    workflow_options = ResolveWorkflowOptions(
        preview=True,  # always resolve from source for build
        rosdep_fallback=rosdep,
    )

    extra_colcon_args = _read_colcon_flagfile(colcon_flagfile) if colcon_flagfile else []

    _run_build(
        package=package,
        launcher=launcher,
        initial_args=initial_args,
        lockfile_path=lockfile,
        src_dir=src,
        workspace_state=workspace_state,
        workflow_options=workflow_options,
        build_base=build_base,
        install_base=install_base,
        log_base=log_base,
        dry_run=dry_run,
        extra_colcon_args=extra_colcon_args,
        test_mode=False,
        verbose=ctx.obj.get("verbose", False),
        shallow=shallow,
    )


# ---------------------------------------------------------------------------
# build-pkg
# ---------------------------------------------------------------------------


@main.command("build-pkg")
@click.argument("packages", nargs=-1, required=True)
@click.option("-l", "--lockfile", default="manifest.lock.repos", help="Lockfile path")
@click.option("--src", default="src", help="Source directory")
@click.option("-c", "--clean", is_flag=True)
@click.option("-d", "--dirty", is_flag=True)
@click.option("--shallow", is_flag=True)
@click.option("--build-base", default="build")
@click.option("--install-base", default="install")
@click.option("--log-base", default="log")
@click.option("--colcon-flagfile", default=None, type=click.Path(exists=True))
@click.option("--rosdep", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def build_pkg(
    ctx: click.Context,
    packages: tuple[str, ...],
    lockfile: str,
    src: str,
    clean: bool,
    dirty: bool,
    shallow: bool,
    build_base: str,
    install_base: str,
    log_base: str,
    colcon_flagfile: str | None,
    rosdep: bool,
    dry_run: bool,
) -> None:
    """Build package(s) by name with automatic transitive dependency fetching."""
    from roscope.builder import BuildOptions, execute_build, plan_build_from_packages
    from roscope.fetcher import FetchOptions, fetch_packages
    from roscope.rosdep import rosdep_install

    parsed_lockfile = _read_lockfile(lockfile)
    src_path = Path(src)
    workspace_state = _parse_workspace_state(clean, dirty)

    fetch_options = FetchOptions(
        recurse_submodules=True,
        shallow=shallow,
        workspace_state=workspace_state,
    )

    # Fetch the seed packages first
    to_fetch = [p for p in packages if p in parsed_lockfile.packages]
    if to_fetch:
        fetch_packages(to_fetch, parsed_lockfile, src_path, fetch_options)

    seed = set(packages)
    plan = plan_build_from_packages(
        seed,
        parsed_lockfile,
        src_path,
        Path(build_base),
        Path(install_base),
        Path(log_base),
        fetch_options,
        test_mode=False,
    )

    extra_colcon_args = _read_colcon_flagfile(colcon_flagfile) if colcon_flagfile else []

    logger.info(
        "Building %d packages (from %d seed): %s",
        len(plan.packages),
        len(seed),
        plan.packages,
    )

    if rosdep and plan.external_deps:
        click.echo(
            f"[build-pkg] Installing {len(plan.external_deps)} external deps via rosdep",
            err=True,
        )
        rosdep_install(sorted(plan.external_deps))

    execute_build(plan, BuildOptions(dry_run=dry_run, extra_colcon_args=extra_colcon_args))


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


@main.command("test")
@click.argument("package")
@click.argument("launcher")
@click.argument("args", nargs=-1)
@click.option("-l", "--lockfile", default="manifest.lock.repos")
@click.option("--src", default="src")
@click.option("-c", "--clean", is_flag=True)
@click.option("-d", "--dirty", is_flag=True)
@click.option("--shallow", is_flag=True)
@click.option("--rosdep", is_flag=True)
@click.option("--build-base", default="build")
@click.option("--install-base", default="install")
@click.option("--log-base", default="log")
@click.option("--colcon-flagfile", default=None, type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True)
@click.pass_context
def test_cmd(
    ctx: click.Context,
    package: str,
    launcher: str,
    args: tuple[str, ...],
    lockfile: str,
    src: str,
    clean: bool,
    dirty: bool,
    shallow: bool,
    rosdep: bool,
    build_base: str,
    install_base: str,
    log_base: str,
    colcon_flagfile: str | None,
    dry_run: bool,
) -> None:
    """Fetch, build, and run tests (includes test_depend packages)."""
    from roscope.orchestrator import ResolveWorkflowOptions

    initial_args = _parse_launch_args(args)
    workspace_state = _parse_workspace_state(clean, dirty)

    workflow_options = ResolveWorkflowOptions(
        preview=True,
        rosdep_fallback=rosdep,
    )

    extra_colcon_args = _read_colcon_flagfile(colcon_flagfile) if colcon_flagfile else []

    _run_build(
        package=package,
        launcher=launcher,
        initial_args=initial_args,
        lockfile_path=lockfile,
        src_dir=src,
        workspace_state=workspace_state,
        workflow_options=workflow_options,
        build_base=build_base,
        install_base=install_base,
        log_base=log_base,
        dry_run=dry_run,
        extra_colcon_args=extra_colcon_args,
        test_mode=True,
        verbose=ctx.obj.get("verbose", False),
        shallow=shallow,
    )


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------


@main.command()
@click.option("--src", default="src", help="Source directory to clean")
@click.option("--keep-git", is_flag=True, help="Keep .git directories for faster re-fetch")
@click.pass_context
def clean(ctx: click.Context, src: str, keep_git: bool) -> None:
    """Clean fetched packages."""
    src_path = Path(src)

    if not src_path.exists():
        click.echo(f"Nothing to clean: {src} does not exist")
        return

    if keep_git:
        logger.info("Cleaning %s (keeping .git directories)", src)
        _clean_directory_keep_git(src_path)
        click.echo(f"Cleaned {src} (kept .git directories for faster re-fetch)")
    else:
        logger.info("Cleaning %s (removing everything)", src)
        shutil.rmtree(src_path)
        click.echo(f"Cleaned {src}")


# ---------------------------------------------------------------------------
# Shared command implementations
# ---------------------------------------------------------------------------


def _cmd_resolve(
    *,
    package: str,
    launcher: str,
    initial_args: dict[str, str],
    lockfile_path: str,
    src_dir: str,
    report: bool,
    preview: bool,
    show_args: bool,
    suppress_xml: bool,
    strict: bool,
    workspace_state: WorkspaceState,
    workflow_options: ResolveWorkflowOptions,
    shallow: bool,
    visualize: bool = False,
) -> None:
    """Shared resolve/check implementation."""
    from roscope.fetcher import FetchOptions
    from roscope.log import DiagnosticCollector
    from roscope.orchestrator import resolve_launch_recursive
    from roscope.renderer import render_resolved_xml

    parsed_lockfile = _read_lockfile(lockfile_path)
    fetch_path = Path(src_dir)
    options = FetchOptions(
        recurse_submodules=True,
        shallow=shallow,
        workspace_state=workspace_state,
    )

    collector = DiagnosticCollector()
    logging.getLogger("roscope").addHandler(collector)
    try:
        result = resolve_launch_recursive(
            parsed_lockfile,
            package,
            launcher,
            fetch_path,
            options=options,
            initial_args=initial_args,
            workflow_options=workflow_options,
        )
    finally:
        logging.getLogger("roscope").removeHandler(collector)

    # Visualize mode: open graph in browser instead of printing XML
    if visualize:
        from launch_plus.visualizer import serve as _visualizer_serve

        _visualizer_serve(result.actions, package, launcher)
        return

    # Render XML to stdout
    if not suppress_xml:
        xml = render_resolved_xml(
            package,
            launcher,
            result.actions,
            show_args=show_args,
            initial_args=result.initial_args,
        )
        if preview:
            xml = "<!-- PREVIEW: resolved from source workspace, not install paths -->\n" + xml
        click.echo(xml)

    # Diagnostics to stderr
    all_errors = collector.errors
    has_diagnostics = bool(all_errors) or bool(collector.warnings)

    if all_errors:
        click.echo(err=True)
        for err in all_errors:
            click.echo(f"[error] {err}", err=True)

    if collector.warnings:
        click.echo(err=True)
        for w in collector.warnings:
            click.echo(f"[warning] {w}", err=True)

    if has_diagnostics:
        click.echo(err=True)
        click.echo(
            f"{len(all_errors)} error(s), {len(collector.warnings)} warning(s).",
            err=True,
        )

    if report:
        click.echo(err=True)
        click.echo("=== Resolution Report ===", err=True)
        click.echo(f"Direct packages:   {len(result.direct_packages)}", err=True)
        click.echo(f"Packages fetched:  {len(result.fetched_packages)}", err=True)
        click.echo(f"Launch files:      {len(result.launch_files)}", err=True)
        click.echo(f"Param files:       {len(result.param_files)}", err=True)
        click.echo(f"Files parsed:      {len(result.parsed_files)}", err=True)

        if result.launch_files:
            click.echo("\nLaunch files:", err=True)
            for f in result.launch_files:
                click.echo(f"  {f.package}:{f.share_path}", err=True)
        if result.param_files:
            click.echo("\nParam files:", err=True)
            for f in result.param_files:
                click.echo(f"  {f.package}:{f.share_path}", err=True)

    # Exit policy
    has_errors = bool(all_errors)
    has_warnings = bool(collector.warnings)
    should_fail = has_errors or (strict and has_warnings) if suppress_xml else has_errors

    if should_fail:
        sys.exit(1)


def _run_build(
    *,
    package: str,
    launcher: str,
    initial_args: dict[str, str],
    lockfile_path: str,
    src_dir: str,
    workspace_state: WorkspaceState,
    workflow_options: ResolveWorkflowOptions,
    build_base: str,
    install_base: str,
    log_base: str,
    dry_run: bool,
    extra_colcon_args: list[str],
    test_mode: bool,
    verbose: bool,
    shallow: bool,
) -> None:
    """Shared build/test implementation."""
    from roscope.builder import BuildOptions, execute_build, plan_build_from_packages
    from roscope.fetcher import FetchOptions
    from roscope.log import DiagnosticCollector
    from roscope.orchestrator import resolve_launch_recursive
    from roscope.rosdep import rosdep_install

    parsed_lockfile = _read_lockfile(lockfile_path)
    fetch_path = Path(src_dir)
    fetch_options = FetchOptions(
        recurse_submodules=True,
        shallow=shallow,
        workspace_state=workspace_state,
    )

    collector = DiagnosticCollector()
    logging.getLogger("roscope").addHandler(collector)
    try:
        result = resolve_launch_recursive(
            parsed_lockfile,
            package,
            launcher,
            fetch_path,
            options=fetch_options,
            initial_args=initial_args,
            workflow_options=workflow_options,
        )
    finally:
        logging.getLogger("roscope").removeHandler(collector)

    if collector.errors:
        for e in collector.errors:
            click.echo(f"[error] {e}", err=True)
        raise click.ClickException("resolve step produced errors; aborting build")

    plan = plan_build_from_packages(
        result.direct_packages,
        parsed_lockfile,
        fetch_path,
        Path(build_base),
        Path(install_base),
        Path(log_base),
        fetch_options,
        test_mode=test_mode,
    )

    suffix = " (+ test deps)" if test_mode else ""
    click.echo(f"[build] {len(plan.packages)} packages to build{suffix}", err=True)
    if verbose:
        for pkg in plan.packages:
            click.echo(f"  {pkg}", err=True)

    if plan.external_deps:
        if workflow_options.rosdep_fallback:
            click.echo(
                f"[build] Installing {len(plan.external_deps)} external deps via rosdep",
                err=True,
            )
            rosdep_install(sorted(plan.external_deps))
        elif verbose:
            click.echo(
                f"[build] {len(plan.external_deps)} external deps not in lockfile "
                f"(use --rosdep to install): {sorted(plan.external_deps)}",
                err=True,
            )

    execute_build(plan, BuildOptions(dry_run=dry_run, extra_colcon_args=extra_colcon_args))


if __name__ == "__main__":
    main()
