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
    /// Reset every repository to the pinned lockfile SHA, discarding any local
    /// modifications (`git checkout -f <sha>`).  If the working tree is dirty,
    /// changes are automatically stashed before checkout.  Guarantees
    /// reproducibility.
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
            if current_sha != sha {
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

    // Checkout if the SHA has changed or new paths were added.
    // In clean mode, checkout uses -f to reset dirty files.
    // If SHA already matches and no new paths were added, skip the checkout
    // regardless of mode — the working tree content is already correct.
    if !sha_matches || !paths_to_add.is_empty() {
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

/// Check if the working tree has uncommitted changes (staged or unstaged).
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

        // After adding new sparse paths we need to checkout to materialize them.
        // Use normal (non-force) checkout at the current SHA.
        let current_sha = get_current_sha(repo_dir)?;
        let output = Command::new("git")
            .current_dir(repo_dir)
            .args(["checkout", &current_sha])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .map_err(|e| crate::Error::Git(format!("failed to run git checkout: {}", e)))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(crate::Error::Git(format!(
                "git checkout {} failed: {}",
                &current_sha[..current_sha.len().min(12)],
                stderr
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

    // In Clean mode, stash any dirty changes before checkout instead of
    // discarding them with -f.
    if options.workspace_state == WorkspaceState::Clean {
        stash_if_dirty(repo_dir)?;
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

    // Update submodules if requested
    if options.recurse_submodules {
        debug!("Updating submodules...");
        match Command::new("git")
            .current_dir(repo_dir)
            .args(["submodule", "update", "--init", "--recursive", "--depth=1"])
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

    #[test]
    fn test_fetch_options_default() {
        let options = FetchOptions::default();
        assert!(options.recurse_submodules);
        assert!(!options.shallow);
    }
}
