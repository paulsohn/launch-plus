"""Sparse-checkout based package fetcher.

Ported from crates/launch-plus-core/src/fetcher.rs.

All git operations are subprocess calls (same as Rust version).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from launch_plus.exceptions import GitError, PackageNotFoundError
from launch_plus.types import Lockfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class FetchedPackage:
    """Result of fetching a single package."""

    name: str
    path: Path


class WorkspaceState(Enum):
    """How the fetcher treats an already-present source workspace."""

    CLEAN = auto()
    """Reset every repository to the pinned lockfile SHA (auto-stash)."""

    DIRTY = auto()
    """Trust whatever is on disk; only missing repos are cloned."""

    DEFAULT = auto()
    """Verify SHA + clean tree; error if mismatch."""


@dataclass
class FetchOptions:
    """Options for fetching packages."""

    recurse_submodules: bool = True
    shallow: bool = False
    workspace_state: WorkspaceState = WorkspaceState.DEFAULT


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
    error_msg: str = "git command failed",
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not in PATH") from None

    if check and result.returncode != 0:
        raise GitError(f"{error_msg}: {result.stderr.strip()}")
    return result


def get_current_sha(repo_dir: Path) -> str:
    """Get the current HEAD SHA of a repository."""
    result = _run_git(
        ["rev-parse", "HEAD"],
        cwd=repo_dir,
        error_msg="git rev-parse HEAD failed",
    )
    return result.stdout.strip()


def is_working_tree_dirty(repo_dir: Path) -> bool:
    """Check if the working tree has uncommitted changes."""
    result = _run_git(
        ["status", "--porcelain"],
        cwd=repo_dir,
        error_msg=f"git status failed in {repo_dir}",
    )
    return bool(result.stdout.strip())


def _is_sparse_checkout_enabled(repo_dir: Path) -> bool:
    result = _run_git(
        ["config", "--get", "core.sparseCheckout"],
        cwd=repo_dir,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _init_sparse_checkout(repo_dir: Path) -> None:
    _run_git(
        ["sparse-checkout", "init", "--no-cone"],
        cwd=repo_dir,
        error_msg="git sparse-checkout init failed",
    )


def _set_sparse_checkout_paths(repo_dir: Path, paths: list[str]) -> None:
    _run_git(
        ["sparse-checkout", "set", "--no-cone", *paths],
        cwd=repo_dir,
        error_msg="git sparse-checkout set failed",
    )


def _add_sparse_checkout_paths(repo_dir: Path, paths: list[str]) -> None:
    _run_git(
        ["sparse-checkout", "add", *paths],
        cwd=repo_dir,
        error_msg="git sparse-checkout add failed",
    )


def _get_sparse_checkout_paths(repo_dir: Path) -> list[str]:
    result = _run_git(
        ["sparse-checkout", "list"],
        cwd=repo_dir,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _describe_repo_state(repo_dir: Path, expected_sha: str) -> str | None:
    """Describe divergence from expected state. Returns None if clean+matching."""
    current_sha = get_current_sha(repo_dir)
    sha_matches = current_sha == expected_sha
    dirty = is_working_tree_dirty(repo_dir)

    if sha_matches and not dirty:
        return None

    reasons: list[str] = []
    if not sha_matches:
        reasons.append(f"SHA mismatch: expected {expected_sha[:12]} but found {current_sha[:12]}")
    if dirty:
        status = _run_git(["status", "--porcelain"], cwd=repo_dir, check=False)
        detail = status.stdout.strip()
        if not detail:
            reasons.append("working tree has uncommitted changes")
        else:
            lines = detail.splitlines()
            if len(lines) > 20:
                shown = "\n    ".join(lines[:20]) + f"\n    ... and {len(lines) - 20} more files"
            else:
                shown = "\n    ".join(lines)
            reasons.append(f"working tree has uncommitted changes:\n    {shown}")

    return f"HEAD is at {current_sha[:12]}\n  " + "\n  ".join(reasons)


def _verify_repo_state(repo_dir: Path, expected_sha: str) -> None:
    """Default mode: error if SHA mismatch or dirty tree."""
    description = _describe_repo_state(repo_dir, expected_sha)
    if description is None:
        return
    repo_name = repo_dir.name
    raise GitError(
        f"repository '{repo_name}' at {repo_dir} is not in the expected state:\n"
        f"  {description}\n\n"
        "Specify how to proceed:\n"
        "  -c, --clean   reset to the lockfile SHA (local changes are stashed; "
        "dirty submodules are discarded)\n"
        "  -d, --dirty   use the current on-disk state as-is"
    )


def _stash_if_dirty(repo_dir: Path, expected_sha: str) -> bool:
    """Stash uncommitted changes. Returns True if stash was created."""
    if not is_working_tree_dirty(repo_dir):
        return False

    description = _describe_repo_state(repo_dir, expected_sha)
    if description:
        logger.info(
            "Stashing changes in %s (clean mode; any dirty submodules will be discarded):\n  %s",
            repo_dir,
            description,
        )
    else:
        logger.info("Stashing changes in %s before clean checkout", repo_dir)

    _run_git(
        [
            "stash",
            "push",
            "--include-untracked",
            "-m",
            "launch-plus auto-stash before clean checkout",
        ],
        cwd=repo_dir,
        error_msg=f"git stash failed in {repo_dir}",
    )
    logger.info("Changes stashed successfully")
    return True


def _checkout_sha(
    repo_dir: Path,
    url: str,
    sha: str,
    options: FetchOptions,
) -> None:
    """Fetch (if needed) and checkout a specific SHA."""
    if options.workspace_state == WorkspaceState.CLEAN:
        _stash_if_dirty(repo_dir, sha)

    # Check if SHA is available locally
    have_locally = _run_git(["cat-file", "-t", sha], cwd=repo_dir, check=False).returncode == 0

    if have_locally:
        logger.debug("SHA %s already available locally, skipping fetch", sha)
    else:
        fetch_args = ["fetch"]
        if options.shallow:
            fetch_args.append("--depth=1")
        fetch_args.extend(["--", url, sha])
        _run_git(
            fetch_args,
            cwd=repo_dir,
            error_msg=f"git fetch of {sha} from {url} failed and commit is not available locally",
        )

    _run_git(
        ["checkout", sha],
        cwd=repo_dir,
        error_msg=f"git checkout {sha} failed",
    )

    # Update submodules if requested
    if options.recurse_submodules:
        sub_args = ["submodule", "update", "--init", "--recursive", "--depth=1"]
        if options.workspace_state == WorkspaceState.CLEAN:
            sub_args.append("--force")
        logger.debug("Updating submodules...")
        result = _run_git(sub_args, cwd=repo_dir, check=False)
        if result.returncode != 0:
            logger.warning("submodule update failed in %s: %s", repo_dir, result.stderr.strip())
        else:
            logger.debug("Submodules updated successfully")


def _add_sparse_paths_if_needed(repo_dir: Path, paths: list[str]) -> None:
    """Add sparse-checkout paths without touching the working tree or SHA."""
    if not _is_sparse_checkout_enabled(repo_dir):
        return
    current = set(_get_sparse_checkout_paths(repo_dir))
    to_add = [p for p in paths if p not in current]
    if not to_add:
        return
    logger.info("Adding paths to sparse-checkout: %s", to_add)
    _add_sparse_checkout_paths(repo_dir, to_add)
    _run_git(
        ["sparse-checkout", "reapply"],
        cwd=repo_dir,
        error_msg="git sparse-checkout reapply failed",
    )


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def _sparse_clone(
    url: str,
    sha: str,
    repo_dir: Path,
    paths: list[str],
    options: FetchOptions,
) -> None:
    """Perform a sparse clone of a repository."""
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Sparse cloning %s into %s (paths: %s)", url, repo_dir, paths)

    clone_args = ["clone", "--filter=blob:none", "--sparse", "--single-branch"]
    if options.shallow:
        clone_args.append("--depth=1")
    if options.recurse_submodules:
        clone_args.extend(["--recurse-submodules", "--shallow-submodules"])
    clone_args.extend([url, str(repo_dir)])

    _run_git(clone_args, error_msg=f"git clone failed for {url}")
    _set_sparse_checkout_paths(repo_dir, paths)
    _checkout_sha(repo_dir, url, sha, options)


def _update_sparse_checkout(
    repo_dir: Path,
    url: str,
    sha: str,
    paths: list[str],
    options: FetchOptions,
) -> None:
    """Update sparse-checkout for an existing repository."""
    logger.debug("Updating sparse-checkout in %s (paths: %s)", repo_dir, paths)

    # Handle no-checkout clone (left by indexer)
    if not (repo_dir / ".git" / "index").exists():
        logger.info("Initializing sparse-checkout for no-checkout clone at %s", repo_dir)
        _init_sparse_checkout(repo_dir)
        _set_sparse_checkout_paths(repo_dir, paths)
        bootstrap = FetchOptions(
            workspace_state=WorkspaceState.DEFAULT,
            recurse_submodules=options.recurse_submodules,
            shallow=options.shallow,
        )
        _checkout_sha(repo_dir, url, sha, bootstrap)
        return

    sparse_enabled = _is_sparse_checkout_enabled(repo_dir)

    if not sparse_enabled:
        current_sha = get_current_sha(repo_dir)
        if current_sha:
            if options.workspace_state == WorkspaceState.DIRTY:
                logger.debug(
                    "Sparse-checkout disabled in %s (current SHA %s), skipping (--dirty)",
                    repo_dir,
                    current_sha[:8],
                )
                return
            need_checkout = current_sha != sha or is_working_tree_dirty(repo_dir)
            if need_checkout:
                logger.info(
                    "Clean mode: resetting %s from %s to %s",
                    repo_dir,
                    current_sha[:8],
                    sha[:8],
                )
                _checkout_sha(repo_dir, url, sha, options)
            else:
                logger.debug("Already at SHA %s (non-sparse repo), skipping checkout", sha)
            return
        # No commits yet
        logger.info("Initializing sparse-checkout with paths: %s", paths)
        _init_sparse_checkout(repo_dir)
        _set_sparse_checkout_paths(repo_dir, paths)
        _checkout_sha(repo_dir, url, sha, options)
        return

    current_sha = get_current_sha(repo_dir)
    sha_matches = current_sha == sha

    current_set = set(_get_sparse_checkout_paths(repo_dir))
    paths_to_add = [p for p in paths if p not in current_set]

    if paths_to_add:
        logger.info("Adding paths to sparse-checkout: %s", paths_to_add)
        _add_sparse_checkout_paths(repo_dir, paths_to_add)

    need_checkout = (
        not sha_matches
        or bool(paths_to_add)
        or (options.workspace_state == WorkspaceState.CLEAN and is_working_tree_dirty(repo_dir))
    )

    if need_checkout:
        _checkout_sha(repo_dir, url, sha, options)
    else:
        logger.debug("Already at SHA %s with correct paths, skipping checkout", sha)


def _ensure_remote_url(repo_dir: Path, url: str) -> None:
    """Sync origin remote URL (imported from indexer in Rust)."""
    result = _run_git(["remote", "get-url", "origin"], cwd=repo_dir, check=False)
    current = result.stdout.strip() if result.returncode == 0 else ""
    if current != url:
        if current:
            _run_git(
                ["remote", "set-url", "origin", url],
                cwd=repo_dir,
                error_msg="failed to set remote url",
            )
        else:
            _run_git(
                ["remote", "add", "origin", url],
                cwd=repo_dir,
                error_msg="failed to add remote",
            )


def fetch_repo_sparse(
    url: str,
    sha: str,
    repo_dir: Path,
    paths: list[str],
    options: FetchOptions,
) -> None:
    """Fetch a repository with sparse-checkout for specific paths."""
    if repo_dir.exists() and (repo_dir / ".git").exists():
        if options.workspace_state == WorkspaceState.DIRTY:
            desc = _describe_repo_state(repo_dir, sha)
            if desc:
                logger.info("Using as-is (--dirty) %s :\n  %s", repo_dir, desc)
            else:
                logger.debug("Skipping git operations for %s (--dirty)", repo_dir)
        elif options.workspace_state == WorkspaceState.DEFAULT:
            _ensure_remote_url(repo_dir, url)
            if not (repo_dir / ".git" / "index").exists():
                logger.info("Initializing sparse-checkout for no-checkout clone at %s", repo_dir)
                _init_sparse_checkout(repo_dir)
                _set_sparse_checkout_paths(repo_dir, paths)
                _checkout_sha(repo_dir, url, sha, options)
            else:
                _verify_repo_state(repo_dir, sha)
                _add_sparse_paths_if_needed(repo_dir, paths)
        else:  # CLEAN
            _ensure_remote_url(repo_dir, url)
            _update_sparse_checkout(repo_dir, url, sha, paths, options)
    else:
        _sparse_clone(url, sha, repo_dir, paths, options)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_packages(
    packages: list[str],
    lockfile: Lockfile,
    fetch_dir: Path,
    options: FetchOptions | None = None,
) -> list[FetchedPackage]:
    """Fetch specified packages from lockfile using sparse-checkout."""
    if options is None:
        options = FetchOptions()

    # Group packages by repository
    repo_packages: dict[str, list[tuple[str, str]]] = {}
    for pkg_name in packages:
        pkg_lock = lockfile.packages.get(pkg_name)
        if pkg_lock is None:
            raise PackageNotFoundError(f"package '{pkg_name}' not found in lockfile")
        repo_packages.setdefault(pkg_lock.repo, []).append((pkg_name, pkg_lock.path))

    fetched: list[FetchedPackage] = []

    for workspace_path in sorted(repo_packages):
        pkg_list = repo_packages[workspace_path]
        repo_lock = lockfile.repositories.get(workspace_path)
        if repo_lock is None:
            raise GitError(f"repository '{workspace_path}' not found in lockfile")

        repo_dir = fetch_dir / workspace_path
        sparse_paths = ["/**" if (p == "." or not p) else p for _, p in pkg_list]

        fetch_repo_sparse(repo_lock.url, repo_lock.version, repo_dir, sparse_paths, options)

        for pkg_name, pkg_path in pkg_list:
            fetched.append(FetchedPackage(name=pkg_name, path=repo_dir / pkg_path))

    return fetched


def fetch_file(
    lockfile: Lockfile,
    package: str,
    share_path: Path | str,
    fetch_dir: Path,
    options: FetchOptions | None = None,
) -> Path:
    """Fetch a specific file from a package using sparse-checkout."""
    if options is None:
        options = FetchOptions()

    pkg_lock = lockfile.packages.get(package)
    if pkg_lock is None:
        raise PackageNotFoundError(f"package '{package}' not found in lockfile")

    repo_lock = lockfile.repositories.get(pkg_lock.repo)
    if repo_lock is None:
        raise GitError(f"repository '{pkg_lock.repo}' not found in lockfile")

    repo_dir = fetch_dir / pkg_lock.repo
    repo_file_path = f"{pkg_lock.path}/{share_path}"

    logger.debug("Fetching file %s:%s -> %s", package, share_path, repo_file_path)

    fetch_repo_sparse(repo_lock.url, repo_lock.version, repo_dir, [repo_file_path], options)

    return repo_dir / pkg_lock.path / share_path


def is_package_fetched(pkg_name: str, lockfile: Lockfile, fetch_dir: Path) -> bool:
    """Check if a package is already fully fetched (has package.xml)."""
    pkg_lock = lockfile.packages.get(pkg_name)
    if pkg_lock is None:
        return False
    pkg_path = fetch_dir / pkg_lock.repo / pkg_lock.path
    return pkg_path.exists() and (pkg_path / "package.xml").exists()


def get_package_path(pkg_name: str, lockfile: Lockfile, fetch_dir: Path) -> Path | None:
    """Get the local path for a fetched package."""
    pkg_lock = lockfile.packages.get(pkg_name)
    if pkg_lock is None:
        return None
    pkg_path = fetch_dir / pkg_lock.repo / pkg_lock.path
    return pkg_path if pkg_path.exists() else None
