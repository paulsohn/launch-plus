//! Fetcher module: Partial git clone via sparse-checkout
//!
//! The fetcher is responsible for:
//! - Fetching only required packages from repositories
//! - Using sparse-checkout to minimize disk usage
//! - Managing fetch cache for already-fetched packages
//! - Supporting shallow clones (depth=1)

use crate::indexer::Lockfile;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use tracing::{debug, info, warn};

/// Result of fetching a single package
#[derive(Debug, Clone)]
pub struct FetchedPackage {
    /// Package name
    pub name: String,
    /// Local path to the fetched package directory
    pub path: PathBuf,
}

/// Controls how the fetcher treats an already-present source workspace.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WorkspaceState {
    /// Reset every repository to the pinned lockfile SHA.  If the working tree
    /// is dirty (including untracked files), changes are automatically stashed
    /// before checkout.  Guarantees reproducibility.
    Clean,
    /// Trust whatever is currently on disk.  Repositories that already exist are
    /// not touched by any git operation.  Only missing repos are cloned fresh.
    Dirty,
    /// Verify that each existing repository matches the lockfile SHA and has a
    /// clean working tree.  If either check fails, error out and ask the user
    /// to explicitly choose `--clean` or `--dirty`.  Missing repos are cloned
    /// fresh (same as the other modes).
    Default,
}

/// Options for fetching packages
#[derive(Debug, Clone)]
pub struct FetchOptions {
    /// Whether to recursively fetch submodules
    pub recurse_submodules: bool,
    /// Whether to use shallow clone (depth=1)
    pub shallow: bool,
    /// How to treat an already-present source workspace
    pub workspace_state: WorkspaceState,
}

impl Default for FetchOptions {
    fn default() -> Self {
        Self {
            recurse_submodules: true,
            shallow: false,
            workspace_state: WorkspaceState::Default,
        }
    }
}

/// Fetch specified packages from lockfile using sparse-checkout
///
/// # Arguments
/// * `packages` - List of package names to fetch
/// * `lockfile` - Lockfile containing package and repository information
/// * `fetch_dir` - Directory to fetch packages into (e.g., `src/`)
/// * `options` - Fetch options (submodules, shallow, etc.)
///
/// # Returns
/// List of fetched packages with their local paths
pub fn fetch_packages(
    packages: &[String],
    lockfile: &Lockfile,
    fetch_dir: &Path,
    options: &FetchOptions,
) -> crate::Result<Vec<FetchedPackage>> {
    // Group packages by repository
    let mut repo_packages: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();

    for pkg_name in packages {
        let pkg_lock = lockfile.packages.get(pkg_name).ok_or_else(|| {
            crate::Error::PackageNotFound(format!("package '{}' not found in lockfile", pkg_name))
        })?;

        repo_packages
            .entry(pkg_lock.repo.clone())
            .or_default()
            .push((pkg_name.clone(), pkg_lock.path.clone()));
    }

    let mut fetched = Vec::new();

    // Fetch each repository
    for (workspace_path, pkg_list) in &repo_packages {
        let repo_lock = lockfile.repositories.get(workspace_path).ok_or_else(|| {
            crate::Error::Git(format!(
                "repository '{}' not found in lockfile",
                workspace_path
            ))
        })?;

        let repo_dir = fetch_dir.join(workspace_path);
        // Normalize sparse-checkout patterns: "." means the package is at the repo root,
        // which in git --no-cone mode needs "/**" to include all files recursively.
        // A bare "." is not a valid gitignore pattern and would produce an empty checkout.
        let sparse_paths: Vec<String> = pkg_list
            .iter()
            .map(|(_, path)| {
                if path == "." || path.is_empty() {
                    "/**".to_string()
                } else {
                    path.clone()
                }
            })
            .collect();
        let paths: Vec<&str> = sparse_paths.iter().map(|s| s.as_str()).collect();

        // Fetch the repository with sparse-checkout
        fetch_repo_sparse(
            &repo_lock.url,
            &repo_lock.version,
            &repo_dir,
            &paths,
            options,
        )?;

        // Add fetched packages to result
        for (pkg_name, pkg_path) in pkg_list {
            fetched.push(FetchedPackage {
                name: pkg_name.clone(),
                path: repo_dir.join(pkg_path),
            });
        }
    }

    Ok(fetched)
}

/// Fetch a specific file from a package using sparse-checkout
///
/// This is used for incremental fetching during launch file resolution.
/// Only the specified file (and its path) is checked out.
///
/// # Arguments
/// * `lockfile` - Lockfile containing package and repository information
/// * `package` - Package name (e.g., "autoware_launch")
/// * `share_path` - Path relative to package share dir (e.g., "launch/autoware.launch.xml")
/// * `fetch_dir` - Directory to fetch into (e.g., `src/`)
/// * `options` - Fetch options
///
/// # Returns
/// Full path to the fetched file
pub fn fetch_file(
    lockfile: &Lockfile,
    package: &str,
    share_path: &Path,
    fetch_dir: &Path,
    options: &FetchOptions,
) -> crate::Result<PathBuf> {
    let pkg_lock = lockfile.packages.get(package).ok_or_else(|| {
        crate::Error::PackageNotFound(format!("package '{}' not found in lockfile", package))
    })?;

    let repo_lock = lockfile.repositories.get(&pkg_lock.repo).ok_or_else(|| {
        crate::Error::Git(format!(
            "repository '{}' not found in lockfile",
            pkg_lock.repo
        ))
    })?;

    let repo_dir = fetch_dir.join(&pkg_lock.repo);

    // Construct the path within the repo: <pkg_path>/<share_path>
    let repo_file_path = format!("{}/{}", pkg_lock.path, share_path.display());

    debug!(
        "Fetching file {}:{} -> {}",
        package,
        share_path.display(),
        repo_file_path
    );

    // Fetch with sparse-checkout
    fetch_repo_sparse(
        &repo_lock.url,
        &repo_lock.version,
        &repo_dir,
        &[&repo_file_path],
        options,
    )?;

    // Return the full path to the file
    Ok(repo_dir.join(&pkg_lock.path).join(share_path))
}

/// Fetch a repository with sparse-checkout for specific paths
fn fetch_repo_sparse(
    url: &str,
    sha: &str,
    repo_dir: &Path,
    paths: &[&str],
    options: &FetchOptions,
) -> crate::Result<()> {
    if repo_dir.exists() && repo_dir.join(".git").exists() {
        match options.workspace_state {
            WorkspaceState::Dirty => {
                // Dirty mode: never touch existing repos — use whatever is on disk.
                debug!(
                    "Skipping git operations for {} (--dirty)",
                    repo_dir.display()
                );
            }
            WorkspaceState::Default => {
                // Default mode: verify SHA + clean working tree, error if mismatch.
                verify_repo_state(repo_dir, sha)?;
                // Verification passed — add any new sparse-checkout paths without
                // resetting the working tree.
                add_sparse_paths_if_needed(repo_dir, paths)?;
            }
            WorkspaceState::Clean => {
                // Clean mode: reset to pinned SHA (with auto-stash).
                update_sparse_checkout(repo_dir, sha, paths, options)?;
            }
        }
    } else {
        // New repository, do sparse clone (workspace_state doesn't apply — nothing on disk yet).
        sparse_clone(url, sha, repo_dir, paths, options)?;
    }

    Ok(())
}

/// Perform a sparse clone of a repository
fn sparse_clone(
    url: &str,
    sha: &str,
    repo_dir: &Path,
    paths: &[&str],
    options: &FetchOptions,
) -> crate::Result<()> {
    // Create parent directories
    if let Some(parent) = repo_dir.parent() {
        std::fs::create_dir_all(parent).map_err(|e| {
            crate::Error::Git(format!(
                "failed to create directory {}: {}",
                parent.display(),
                e
            ))
        })?;
    }

    info!(
        "Sparse cloning {} into {} (paths: {:?})",
        url,
        repo_dir.display(),
        paths
    );

    // Build clone arguments
    let mut clone_args = vec!["clone", "--filter=blob:none", "--sparse", "--single-branch"];

    if options.shallow {
        clone_args.push("--depth=1");
    }

    if options.recurse_submodules {
        clone_args.push("--recurse-submodules");
        clone_args.push("--shallow-submodules");
    }

    clone_args.push(url);
    clone_args.push(repo_dir.to_str().unwrap());

    // Clone the repository
    let output = Command::new("git")
        .args(&clone_args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git clone: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git clone failed for {}: {}",
            url, stderr
        )));
    }

    // Set sparse-checkout paths
    set_sparse_checkout_paths(repo_dir, paths)?;

    // Fetch and checkout the specific SHA
    checkout_sha(repo_dir, sha, options)?;

    Ok(())
}

/// Update sparse-checkout for an existing repository
fn update_sparse_checkout(
    repo_dir: &Path,
    sha: &str,
    paths: &[&str],
    options: &FetchOptions,
) -> crate::Result<()> {
    debug!(
        "Updating sparse-checkout in {} (paths: {:?})",
        repo_dir.display(),
        paths
    );

    let sparse_enabled = is_sparse_checkout_enabled(repo_dir);

    if !sparse_enabled {
        let current_sha = get_current_sha(repo_dir).unwrap_or_default();
        if !current_sha.is_empty() {
            if options.workspace_state == WorkspaceState::Dirty {
                // Dirty mode: leave the working tree untouched.
                debug!(
                    "Sparse-checkout disabled in {} (current SHA {}), skipping (--dirty)",
                    repo_dir.display(),
                    &current_sha[..current_sha.len().min(8)],
                );
                return Ok(());
            }
            // Clean mode: reset to the pinned SHA even for non-sparse repos.
            // Also stash + re-checkout if the tree is dirty (even at the correct SHA).
            let need_checkout = current_sha != sha || is_working_tree_dirty(repo_dir)?;
            if need_checkout {
                info!(
                    "Clean mode: resetting {} from {} to {}",
                    repo_dir.display(),
                    &current_sha[..current_sha.len().min(8)],
                    &sha[..sha.len().min(8)],
                );
                checkout_sha(repo_dir, sha, options)?;
            } else {
                debug!(
                    "Already at SHA {} (non-sparse repo), skipping checkout",
                    sha
                );
            }
            return Ok(());
        }
        // No commits yet — fresh setup: initialize sparse-checkout.
        info!("Initializing sparse-checkout with paths: {:?}", paths);
        init_sparse_checkout(repo_dir)?;
        set_sparse_checkout_paths(repo_dir, paths)?;
        checkout_sha(repo_dir, sha, options)?;
        return Ok(());
    }

    let current_sha = get_current_sha(repo_dir).unwrap_or_default();
    let sha_matches = current_sha == sha;

    let current_paths = get_sparse_checkout_paths(repo_dir)?;
    let new_paths: BTreeSet<&str> = paths.iter().copied().collect();
    let current_set: BTreeSet<&str> = current_paths.iter().map(|s| s.as_str()).collect();

    // Sparse-checkout paths are purely additive: packages already on disk must never
    // be removed by a subsequent fetch call that only lists a different subset.
    let paths_to_add: Vec<&str> = new_paths.difference(&current_set).copied().collect();

    // Update sparse-checkout rules if paths changed.
    if !paths_to_add.is_empty() {
        info!("Adding paths to sparse-checkout: {:?}", paths_to_add);
        add_sparse_checkout_paths(repo_dir, &paths_to_add)?;
    }

    // Checkout when: SHA changed, new paths added, or clean mode with dirty tree.
    // Clean mode must guarantee a clean working tree, so even at the correct SHA
    // we stash + re-checkout if the tree is dirty.
    let need_checkout = !sha_matches
        || !paths_to_add.is_empty()
        || (options.workspace_state == WorkspaceState::Clean && is_working_tree_dirty(repo_dir)?);

    if need_checkout {
        checkout_sha(repo_dir, sha, options)?;
    } else {
        debug!(
            "Already at SHA {} with correct paths, skipping checkout",
            sha
        );
    }

    Ok(())
}

/// Get the current HEAD SHA of a repository
fn get_current_sha(repo_dir: &Path) -> crate::Result<String> {
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["rev-parse", "HEAD"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git rev-parse HEAD: {}", e)))?;

    if !output.status.success() {
        return Err(crate::Error::Git("git rev-parse HEAD failed".to_string()));
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Check if sparse-checkout is enabled for a repository
fn is_sparse_checkout_enabled(repo_dir: &Path) -> bool {
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["config", "--get", "core.sparseCheckout"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output();

    if let Ok(output) = output {
        if output.status.success() {
            let stdout = String::from_utf8_lossy(&output.stdout);
            return stdout.trim() == "true";
        }
    }
    false
}

/// Initialize sparse-checkout for a repository
fn init_sparse_checkout(repo_dir: &Path) -> crate::Result<()> {
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["sparse-checkout", "init", "--no-cone"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git sparse-checkout init: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git sparse-checkout init failed: {}",
            stderr
        )));
    }

    Ok(())
}

/// Set sparse-checkout paths (replaces existing)
fn set_sparse_checkout_paths(repo_dir: &Path, paths: &[&str]) -> crate::Result<()> {
    let mut args = vec!["sparse-checkout", "set", "--no-cone"];
    args.extend(paths);

    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(&args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git sparse-checkout set: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git sparse-checkout set failed: {}",
            stderr
        )));
    }

    Ok(())
}

/// Add paths to sparse-checkout (keeps existing)
fn add_sparse_checkout_paths(repo_dir: &Path, paths: &[&str]) -> crate::Result<()> {
    let mut args = vec!["sparse-checkout", "add"];
    args.extend(paths);

    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(&args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git sparse-checkout add: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git sparse-checkout add failed: {}",
            stderr
        )));
    }

    Ok(())
}

/// Get current sparse-checkout paths
fn get_sparse_checkout_paths(repo_dir: &Path) -> crate::Result<Vec<String>> {
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["sparse-checkout", "list"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git sparse-checkout list: {}", e)))?;

    if !output.status.success() {
        // sparse-checkout list may fail if not initialized, return empty
        return Ok(Vec::new());
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(stdout.lines().map(|s| s.to_string()).collect())
}

/// Check if the working tree has uncommitted changes (staged, unstaged, or untracked files).
fn is_working_tree_dirty(repo_dir: &Path) -> crate::Result<bool> {
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["status", "--porcelain"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git status: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git status failed in {}: {}",
            repo_dir.display(),
            stderr.trim()
        )));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(!stdout.trim().is_empty())
}

/// Default mode: verify that the repo is at the expected SHA and has a clean
/// working tree.  Returns an error with actionable guidance if either check fails.
fn verify_repo_state(repo_dir: &Path, expected_sha: &str) -> crate::Result<()> {
    let current_sha = get_current_sha(repo_dir)?;
    let sha_matches = current_sha == expected_sha;
    let dirty = is_working_tree_dirty(repo_dir)?;

    if sha_matches && !dirty {
        return Ok(());
    }

    let repo_name = repo_dir.file_name().unwrap_or_default().to_string_lossy();

    let mut reasons = Vec::new();
    if !sha_matches {
        reasons.push(format!(
            "SHA mismatch: expected {} but found {}",
            &expected_sha[..expected_sha.len().min(12)],
            &current_sha[..current_sha.len().min(12)],
        ));
    }
    if dirty {
        reasons.push("working tree has uncommitted changes".to_string());
    }

    Err(crate::Error::Git(format!(
        "repository '{}' at {} is not in the expected state:\n  {}\n\n\
         Specify how to proceed:\n  \
         -c, --clean   reset to the lockfile SHA (local changes are stashed)\n  \
         -d, --dirty   use the current on-disk state as-is",
        repo_name,
        repo_dir.display(),
        reasons.join("\n  "),
    )))
}

/// Add sparse-checkout paths without touching the working tree or SHA.
/// Used by Default mode after verification passes.
fn add_sparse_paths_if_needed(repo_dir: &Path, paths: &[&str]) -> crate::Result<()> {
    if !is_sparse_checkout_enabled(repo_dir) {
        return Ok(());
    }

    let current_paths = get_sparse_checkout_paths(repo_dir)?;
    let new_paths: BTreeSet<&str> = paths.iter().copied().collect();
    let current_set: BTreeSet<&str> = current_paths.iter().map(|s| s.as_str()).collect();
    let paths_to_add: Vec<&str> = new_paths.difference(&current_set).copied().collect();

    if !paths_to_add.is_empty() {
        info!("Adding paths to sparse-checkout: {:?}", paths_to_add);
        add_sparse_checkout_paths(repo_dir, &paths_to_add)?;

        // Reapply sparse-checkout to materialize the newly added paths
        // without detaching HEAD or changing the checked-out commit.
        let output = Command::new("git")
            .current_dir(repo_dir)
            .args(["sparse-checkout", "reapply"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .map_err(|e| {
                crate::Error::Git(format!("failed to run git sparse-checkout reapply: {}", e))
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(crate::Error::Git(format!(
                "git sparse-checkout reapply failed: {}",
                stderr.trim()
            )));
        }
    }

    Ok(())
}

/// Stash any uncommitted changes in the working tree.
/// Returns `true` if a stash was created, `false` if the tree was already clean.
fn stash_if_dirty(repo_dir: &Path) -> crate::Result<bool> {
    if !is_working_tree_dirty(repo_dir)? {
        return Ok(false);
    }

    info!(
        "Stashing uncommitted changes in {} before clean checkout",
        repo_dir.display()
    );

    let output = Command::new("git")
        .current_dir(repo_dir)
        .args([
            "stash",
            "push",
            "--include-untracked",
            "-m",
            "launch-plus auto-stash before clean checkout",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git stash: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git stash failed in {}: {}",
            repo_dir.display(),
            stderr.trim()
        )));
    }

    info!("Changes stashed successfully");
    Ok(true)
}

/// Checkout a specific SHA
fn checkout_sha(repo_dir: &Path, sha: &str, options: &FetchOptions) -> crate::Result<()> {
    // In clean mode, stash dirty changes first — before any other git operations.
    if options.workspace_state == WorkspaceState::Clean {
        stash_if_dirty(repo_dir)?;
    }

    // Check if the SHA is already available locally before fetching.
    let have_locally = Command::new("git")
        .current_dir(repo_dir)
        .args(["cat-file", "-t", sha])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);

    if have_locally {
        debug!("SHA {} already available locally, skipping fetch", sha);
    } else {
        let mut fetch_args = vec!["fetch", "origin", sha];
        if options.shallow {
            fetch_args.insert(1, "--depth=1");
        }

        let output = Command::new("git")
            .current_dir(repo_dir)
            .args(&fetch_args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .map_err(|e| crate::Error::Git(format!("failed to run git fetch: {}", e)))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(crate::Error::Git(format!(
                "git fetch of {} failed and commit is not available locally: {}",
                sha,
                stderr.trim()
            )));
        }
    }

    let checkout_args: &[&str] = &["checkout", sha];
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(checkout_args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git checkout: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git checkout {} failed: {}",
            sha, stderr
        )));
    }

    // Update submodules if requested.
    // In clean mode, use --force to reset dirty submodules (stash_if_dirty only
    // handles the superproject; git stash does not cover submodule changes).
    if options.recurse_submodules {
        debug!("Updating submodules...");
        let mut sub_args = vec!["submodule", "update", "--init", "--recursive", "--depth=1"];
        if options.workspace_state == WorkspaceState::Clean {
            sub_args.push("--force");
        }
        match Command::new("git")
            .current_dir(repo_dir)
            .args(&sub_args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
        {
            Ok(output) if !output.status.success() => {
                let stderr = String::from_utf8_lossy(&output.stderr);
                warn!(
                    "submodule update failed in {}: {}",
                    repo_dir.display(),
                    stderr.trim()
                );
            }
            Err(e) => warn!("failed to run git submodule update: {e}"),
            Ok(_) => debug!("Submodules updated successfully"),
        }
    }

    Ok(())
}

/// Check if a package is already fetched
pub fn is_package_fetched(pkg_name: &str, lockfile: &Lockfile, fetch_dir: &Path) -> bool {
    let Some(pkg_lock) = lockfile.packages.get(pkg_name) else {
        return false;
    };

    let pkg_path = fetch_dir.join(&pkg_lock.repo).join(&pkg_lock.path);
    pkg_path.exists() && pkg_path.join("package.xml").exists()
}

/// Get the local path for a fetched package
pub fn get_package_path(pkg_name: &str, lockfile: &Lockfile, fetch_dir: &Path) -> Option<PathBuf> {
    let pkg_lock = lockfile.packages.get(pkg_name)?;
    let pkg_path = fetch_dir.join(&pkg_lock.repo).join(&pkg_lock.path);

    if pkg_path.exists() {
        Some(pkg_path)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// Create a temporary git repo with one commit, return (TempDir, sha).
    fn setup_test_repo() -> (tempfile::TempDir, String) {
        let dir = tempfile::tempdir().unwrap();
        let run = |args: &[&str]| {
            let output = Command::new("git")
                .current_dir(dir.path())
                .args(args)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "git {:?} failed: {}",
                args,
                String::from_utf8_lossy(&output.stderr)
            );
        };
        run(&["init", "-b", "main"]);
        run(&["config", "user.email", "test@test.com"]);
        run(&["config", "user.name", "Test"]);
        run(&["config", "commit.gpgsign", "false"]);
        fs::write(dir.path().join("file.txt"), "hello").unwrap();
        run(&["add", "."]);
        run(&["commit", "-m", "initial"]);
        let sha = get_current_sha(dir.path()).unwrap();
        (dir, sha)
    }

    /// Add a second commit to the repo, return the new SHA.
    fn add_commit(dir: &Path, filename: &str, content: &str) -> String {
        fs::write(dir.join(filename), content).unwrap();
        let run = |args: &[&str]| {
            let output = Command::new("git")
                .current_dir(dir)
                .args(args)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output()
                .unwrap();
            assert!(output.status.success());
        };
        run(&["add", "."]);
        run(&["commit", "-m", &format!("add {filename}")]);
        get_current_sha(dir).unwrap()
    }

    #[test]
    fn test_fetch_options_default() {
        let options = FetchOptions::default();
        assert!(options.recurse_submodules);
        assert!(!options.shallow);
        assert_eq!(options.workspace_state, WorkspaceState::Default);
    }

    // ── is_working_tree_dirty ────────────────────────────────────────────

    #[test]
    fn test_clean_tree_is_not_dirty() {
        let (dir, _) = setup_test_repo();
        assert!(!is_working_tree_dirty(dir.path()).unwrap());
    }

    #[test]
    fn test_modified_file_is_dirty() {
        let (dir, _) = setup_test_repo();
        fs::write(dir.path().join("file.txt"), "modified").unwrap();
        assert!(is_working_tree_dirty(dir.path()).unwrap());
    }

    #[test]
    fn test_staged_file_is_dirty() {
        let (dir, _) = setup_test_repo();
        fs::write(dir.path().join("file.txt"), "staged").unwrap();
        Command::new("git")
            .current_dir(dir.path())
            .args(["add", "file.txt"])
            .output()
            .unwrap();
        assert!(is_working_tree_dirty(dir.path()).unwrap());
    }

    #[test]
    fn test_untracked_file_is_dirty() {
        let (dir, _) = setup_test_repo();
        fs::write(dir.path().join("new.txt"), "untracked").unwrap();
        assert!(is_working_tree_dirty(dir.path()).unwrap());
    }

    // ── verify_repo_state ────────────────────────────────────────────────

    #[test]
    fn test_verify_repo_state_ok() {
        let (dir, sha) = setup_test_repo();
        assert!(verify_repo_state(dir.path(), &sha).is_ok());
    }

    #[test]
    fn test_verify_repo_state_sha_mismatch() {
        let (dir, _) = setup_test_repo();
        let err =
            verify_repo_state(dir.path(), "0000000000000000000000000000000000000000").unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("SHA mismatch"), "unexpected error: {msg}");
    }

    #[test]
    fn test_verify_repo_state_dirty_tree() {
        let (dir, sha) = setup_test_repo();
        fs::write(dir.path().join("file.txt"), "dirty").unwrap();
        let err = verify_repo_state(dir.path(), &sha).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("uncommitted changes"),
            "unexpected error: {msg}"
        );
    }

    #[test]
    fn test_verify_repo_state_both_wrong() {
        let (dir, _) = setup_test_repo();
        fs::write(dir.path().join("file.txt"), "dirty").unwrap();
        let err =
            verify_repo_state(dir.path(), "0000000000000000000000000000000000000000").unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("SHA mismatch"), "missing SHA mismatch: {msg}");
        assert!(
            msg.contains("uncommitted changes"),
            "missing dirty warning: {msg}"
        );
    }

    // ── stash_if_dirty ───────────────────────────────────────────────────

    #[test]
    fn test_stash_clean_tree_returns_false() {
        let (dir, _) = setup_test_repo();
        assert!(!stash_if_dirty(dir.path()).unwrap());
    }

    #[test]
    fn test_stash_modified_file() {
        let (dir, _) = setup_test_repo();
        fs::write(dir.path().join("file.txt"), "dirty").unwrap();
        assert!(stash_if_dirty(dir.path()).unwrap());
        assert!(!is_working_tree_dirty(dir.path()).unwrap());
        // Original content restored
        assert_eq!(
            fs::read_to_string(dir.path().join("file.txt")).unwrap(),
            "hello"
        );
    }

    #[test]
    fn test_stash_untracked_file() {
        let (dir, _) = setup_test_repo();
        fs::write(dir.path().join("new.txt"), "untracked").unwrap();
        assert!(stash_if_dirty(dir.path()).unwrap());
        assert!(!is_working_tree_dirty(dir.path()).unwrap());
        assert!(!dir.path().join("new.txt").exists());
    }

    // ── checkout_sha ─────────────────────────────────────────────────────

    #[test]
    fn test_checkout_sha_local() {
        let (dir, sha1) = setup_test_repo();
        let _sha2 = add_commit(dir.path(), "second.txt", "world");

        // Checkout back to sha1 — SHA is local so no fetch needed.
        let options = FetchOptions {
            workspace_state: WorkspaceState::Dirty,
            ..Default::default()
        };
        checkout_sha(dir.path(), &sha1, &options).unwrap();
        assert_eq!(get_current_sha(dir.path()).unwrap(), sha1);
    }

    #[test]
    fn test_checkout_sha_clean_mode_stashes_first() {
        let (dir, sha1) = setup_test_repo();
        let _sha2 = add_commit(dir.path(), "second.txt", "world");

        // Make the tree dirty at sha2
        fs::write(dir.path().join("second.txt"), "modified").unwrap();
        assert!(is_working_tree_dirty(dir.path()).unwrap());

        // Clean mode: should stash, then checkout to sha1
        let options = FetchOptions {
            workspace_state: WorkspaceState::Clean,
            ..Default::default()
        };
        checkout_sha(dir.path(), &sha1, &options).unwrap();
        assert_eq!(get_current_sha(dir.path()).unwrap(), sha1);
        assert!(!is_working_tree_dirty(dir.path()).unwrap());
    }
}
