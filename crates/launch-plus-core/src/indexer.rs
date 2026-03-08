//! Indexer module: Parse .repos files and generate lockfiles
//!
//! The indexer is responsible for:
//! - Parsing .repos YAML files (vcs format)
//! - Resolving versions (tags/branches/SHAs) to concrete SHAs via blobless clone
//! - Fetching package.xml files from git objects (no working tree needed)
//! - Generating dual-indexed lockfiles (repos + packages)

use quick_xml::Reader;
use quick_xml::events::Event;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::io::Read as IoRead;
use std::path::Path;
use std::process::{Command, Stdio};
use tracing::{debug, warn};

/// A .repos file in vcs format
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReposFile {
    /// Map of repository path to repository entry
    pub repositories: BTreeMap<String, RepoEntry>,
}

/// A single repository entry in a .repos file
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoEntry {
    /// Repository type (always "git" for our purposes)
    #[serde(rename = "type")]
    pub repo_type: String,

    /// Git repository URL
    pub url: String,

    /// Version specification (tag, branch, or SHA)
    pub version: String,
}

/// The lockfile format with dual indexing
///
/// This format is VCS-compatible: `vcs import src < manifest.lock.repos`
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Lockfile {
    /// Repository-centric view (for fetching)
    /// Named "repositories" for VCS compatibility
    /// Uses BTreeMap for deterministic ordering in output
    pub repositories: BTreeMap<String, RepoLock>,

    /// Package-centric view (for O(1) lookup)
    /// Uses BTreeMap for deterministic ordering in output
    pub packages: BTreeMap<String, PackageLock>,
}

impl Default for Lockfile {
    fn default() -> Self {
        Self::new()
    }
}

impl Lockfile {
    /// Create a new empty lockfile
    #[must_use]
    pub fn new() -> Self {
        Self {
            repositories: BTreeMap::new(),
            packages: BTreeMap::new(),
        }
    }

    /// Remove a repository and all its packages from the lockfile
    ///
    /// This is used for atomic updates - remove old entries before re-adding
    /// to handle package migrations between repositories correctly.
    pub fn remove_repo(&mut self, workspace_path: &str) {
        if let Some(repo) = self.repositories.remove(workspace_path) {
            // Remove all packages that belonged to this repo
            for pkg_name in &repo.packages {
                self.packages.remove(pkg_name);
            }
        }
    }
}

/// Repository lock entry (VCS-compatible format)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoLock {
    /// Repository type (always "git" for VCS compatibility)
    #[serde(rename = "type")]
    pub repo_type: String,

    /// Git repository URL
    pub url: String,

    /// Pinned SHA (used by VCS for checkout)
    pub version: String,

    /// Original version reference from .repos (tag, branch, or short SHA)
    /// Used by `update` command to track and resolve new versions.
    /// Omitted if the original version was already a full SHA (nothing to update).
    #[serde(rename = "ref", skip_serializing_if = "Option::is_none")]
    pub version_ref: Option<String>,

    /// List of package names in this repository (launch-plus metadata)
    pub packages: Vec<String>,
}

/// Package lock entry
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PackageLock {
    /// Reference to repository key in repos map
    pub repo: String,

    /// Path within the repository
    pub path: String,

    /// Legacy: ignored during deserialization for backward-compatible lockfile reading.
    #[serde(default, skip_serializing)]
    pub dependencies: Dependencies,
}

/// Package dependencies categorized by REP-149 tag type.
///
/// Parsed from on-disk `package.xml` at resolve/build time — NOT stored in lockfile.
///
/// `<depend>` expands to `build + build_export + exec` (REP-149 §3).
///
/// For colcon build ordering, all of `build + build_export + buildtool +
/// buildtool_export` form the "must-build-before" set.
#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
pub struct Dependencies {
    /// `<build_depend>`: needed to compile this package.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub build: Vec<String>,

    /// `<build_export_depend>`: needed by packages that compile against this one.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub build_export: Vec<String>,

    /// `<buildtool_depend>`: build tool needed to build this package (e.g. ament_cmake).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub buildtool: Vec<String>,

    /// `<buildtool_export_depend>`: build tool exported by this package for downstream.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub buildtool_export: Vec<String>,

    /// `<exec_depend>`: needed at runtime.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub exec: Vec<String>,

    /// `<test_depend>`: needed only for testing.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub test: Vec<String>,
}

/// Parse a .repos file from YAML content
///
/// # Errors
///
/// Returns an error if the YAML is malformed or doesn't match the .repos format
pub fn parse_repos(content: &str) -> crate::Result<ReposFile> {
    serde_yaml::from_str(content).map_err(Into::into)
}

/// Parse a lockfile from YAML content
///
/// # Errors
///
/// Returns an error if the YAML is malformed or doesn't match the lockfile format
pub fn parse_lockfile(content: &str) -> crate::Result<Lockfile> {
    serde_yaml::from_str(content).map_err(Into::into)
}

/// Lockfile header comment
const LOCKFILE_HEADER: &str = r#"# launch-plus lockfile (VCS-compatible)
#
# This file is auto-generated by `launch-plus index`. Do not edit manually.
# To regenerate, run: launch-plus index <your.repos>
#
# VCS import: vcs import src < manifest.lock.repos
#
# This lockfile pins exact commit SHAs for all repositories in your .repos file,
# ensuring reproducible builds. It contains:
#   - repositories: Repository metadata with pinned SHAs (VCS-compatible)
#   - packages: Package index for O(1) dependency lookup
#
"#;

/// Serialize a lockfile to YAML with header comment
///
/// # Errors
///
/// Returns an error if serialization fails
pub fn serialize_lockfile(lockfile: &Lockfile) -> crate::Result<String> {
    let yaml = serde_yaml::to_string(lockfile)?;
    Ok(format!("{}{}", LOCKFILE_HEADER, yaml))
}

/// Resolve a ref to a SHA using a local clone via `git fetch`
///
/// Preferred over `resolve_version` when a local clone exists: works with
/// shallow/sparse repos and avoids an extra round-trip for repos that don't
/// support `ls-remote`.
///
/// # Arguments
/// * `repo_dir` - Path to local git clone
/// * `version_ref` - Branch or tag name to resolve
///
/// # Returns
/// The full 40-character SHA of the fetched ref (via `FETCH_HEAD`)
pub fn resolve_version_local(repo_dir: &Path, version_ref: &str) -> crate::Result<String> {
    // Fetch the specific ref from origin
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["fetch", "origin", version_ref])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git fetch: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git fetch origin {version_ref} failed: {stderr}"
        )));
    }

    // FETCH_HEAD points to the SHA of the ref just fetched
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["rev-parse", "FETCH_HEAD"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git rev-parse FETCH_HEAD: {e}")))?;

    if !output.status.success() {
        return Err(crate::Error::Git(
            "git rev-parse FETCH_HEAD failed".to_string(),
        ));
    }

    let sha = String::from_utf8_lossy(&output.stdout).trim().to_string();
    debug!("Resolved {} to SHA {} via local fetch", version_ref, sha);
    Ok(sha)
}

/// Information about a package found in a repository
#[derive(Debug, Clone)]
pub struct PackageInfo {
    /// Package name from package.xml
    pub name: String,
    /// Path within the repository (directory containing package.xml)
    pub path: String,
    /// Dependencies extracted from package.xml
    pub dependencies: Dependencies,
}

/// Parse package.xml content to extract package information
///
/// # Arguments
/// * `content` - The XML content of package.xml
/// * `path` - The path within the repository where this package.xml was found
///
/// # Returns
/// `PackageInfo` with name and dependencies
pub fn parse_package_xml(content: &str, path: &str) -> crate::Result<PackageInfo> {
    let mut reader = Reader::from_str(content);
    reader.config_mut().trim_text(true);

    let mut name = String::new();
    let mut dependencies = Dependencies::default();
    let mut current_tag = String::new();
    let mut buf = Vec::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                current_tag = String::from_utf8_lossy(e.name().as_ref()).to_string();
            }
            Ok(Event::Text(e)) => {
                let text = e.unescape().map_err(|err| {
                    crate::Error::XmlParse(format!("failed to unescape text: {err}"))
                })?;

                let dep = text.trim().to_string();
                if dep.is_empty() {
                    // skip empty text nodes
                } else {
                    match current_tag.as_str() {
                        "name" if name.is_empty() => {
                            name = dep;
                        }
                        "build_depend" => {
                            if !dependencies.build.contains(&dep) {
                                dependencies.build.push(dep);
                            }
                        }
                        "build_export_depend" => {
                            if !dependencies.build_export.contains(&dep) {
                                dependencies.build_export.push(dep);
                            }
                        }
                        "buildtool_depend" => {
                            if !dependencies.buildtool.contains(&dep) {
                                dependencies.buildtool.push(dep);
                            }
                        }
                        "buildtool_export_depend" => {
                            if !dependencies.buildtool_export.contains(&dep) {
                                dependencies.buildtool_export.push(dep);
                            }
                        }
                        "exec_depend" => {
                            if !dependencies.exec.contains(&dep) {
                                dependencies.exec.push(dep);
                            }
                        }
                        "test_depend" => {
                            if !dependencies.test.contains(&dep) {
                                dependencies.test.push(dep);
                            }
                        }
                        // REP-149: <depend> = build_depend + build_export_depend + exec_depend
                        "depend" => {
                            if !dependencies.build.contains(&dep) {
                                dependencies.build.push(dep.clone());
                            }
                            if !dependencies.build_export.contains(&dep) {
                                dependencies.build_export.push(dep.clone());
                            }
                            if !dependencies.exec.contains(&dep) {
                                dependencies.exec.push(dep);
                            }
                        }
                        _ => {}
                    }
                }
            }
            Ok(Event::End(_)) => {
                current_tag.clear();
            }
            Ok(Event::Eof) => break,
            Err(e) => {
                return Err(crate::Error::XmlParse(format!(
                    "error parsing package.xml at position {}: {e}",
                    reader.error_position()
                )));
            }
            _ => {}
        }
        buf.clear();
    }

    if name.is_empty() {
        return Err(crate::Error::XmlParse(
            "package.xml missing <name> element".to_string(),
        ));
    }

    dependencies.build.sort();
    dependencies.build_export.sort();
    dependencies.buildtool.sort();
    dependencies.buildtool_export.sort();
    dependencies.exec.sort();
    dependencies.test.sort();

    Ok(PackageInfo {
        name,
        path: path.to_string(),
        dependencies,
    })
}

/// Discover all packages in a repository and parse their package.xml files.
///
/// Reads package.xml content directly from git objects — no working tree is written.
/// Three strategies are tried in order:
///   1. Existing local repo at `repo_dir`: inspect objects via `git ls-tree` + `git show`
///   2. `git archive --remote`: fetch the tree as a tar stream (requires server support)
///   3. Blobless clone into `repo_dir`: `git clone --filter=blob:none --no-checkout`,
///      then inspect objects as in strategy 1
///
/// # Arguments
/// * `url` - Git repository URL
/// * `sha` - Commit SHA to read
/// * `repo_dir` - Directory where the repository lives or will be cloned.
///   Required for strategies 1 and 3; strategy 2 runs without it.
/// * `recurse_submodules` - Whether to include packages from submodules
pub fn discover_packages(
    url: &str,
    sha: &str,
    repo_dir: Option<&Path>,
    recurse_submodules: bool,
) -> crate::Result<Vec<PackageInfo>> {
    // If repo_dir is specified and exists, use it directly
    if let Some(dir) = repo_dir {
        if dir.exists() && dir.join(".git").exists() {
            debug!("Using existing repo at {}", dir.display());
            return discover_packages_from_existing_repo(dir, sha, recurse_submodules);
        }
    }

    // Try git archive (requires the server to support git-upload-archive).
    // Skipped when recurse_submodules is true because git archive does not support submodules.
    if !recurse_submodules {
        let output = Command::new("git")
            .args(["archive", "--remote", url, sha])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output();

        if let Ok(output) = output {
            if output.status.success() {
                return discover_packages_from_tar(&output.stdout);
            }
        }
    }

    // Blobless no-checkout clone into repo_dir.
    // --filter=blob:none fetches tree/commit objects but no file blobs, so no working
    // tree is written.  Individual blobs (package.xml files) are fetched on demand by
    // `discover_packages_from_existing_repo` via `git show`.
    let dir = repo_dir.ok_or_else(|| {
        crate::Error::Git(
            "a source directory (--src) is required to clone repositories".to_string(),
        )
    })?;

    blobless_clone(url, dir)?;
    discover_packages_from_existing_repo(dir, sha, recurse_submodules)
}

/// Discover packages from a tar archive (git archive output)
fn discover_packages_from_tar(tar_data: &[u8]) -> crate::Result<Vec<PackageInfo>> {
    use std::io::Cursor;

    let cursor = Cursor::new(tar_data);
    let mut archive = tar::Archive::new(cursor);
    let mut packages = Vec::new();

    for entry in archive
        .entries()
        .map_err(|e| crate::Error::Git(format!("failed to read tar archive: {e}")))?
    {
        let mut entry =
            entry.map_err(|e| crate::Error::Git(format!("failed to read tar entry: {e}")))?;

        let path = entry
            .path()
            .map_err(|e| crate::Error::Git(format!("failed to get path: {e}")))?
            .into_owned();

        let path_str = path.to_string_lossy().to_string();
        if path_str.ends_with("/package.xml") || path_str == "package.xml" {
            let mut content = String::new();
            if entry.read_to_string(&mut content).is_ok() {
                let pkg_path = if let Some(parent) = path.parent() {
                    let p = parent.to_string_lossy().to_string();
                    if p.is_empty() { ".".to_string() } else { p }
                } else {
                    ".".to_string()
                };

                match parse_package_xml(&content, &pkg_path) {
                    Ok(info) => {
                        debug!("Found package '{}' at {}", info.name, pkg_path);
                        packages.push(info);
                    }
                    Err(e) => {
                        warn!("Failed to parse {}: {}", path_str, e);
                    }
                }
            }
        }
    }

    debug!("Found {} packages from archive", packages.len());
    Ok(packages)
}

/// Discover packages from an existing git repository using git object inspection
///
/// This function reads package.xml files directly from git objects without
/// modifying the working directory. This is safe for sparse-checkout repos.
fn discover_packages_from_existing_repo(
    repo_dir: &Path,
    sha: &str,
    recurse_submodules: bool,
) -> crate::Result<Vec<PackageInfo>> {
    // Fetch the SHA (in case it's not available locally)
    debug!("Fetching {} in existing repo...", sha);
    let fetch_result = Command::new("git")
        .current_dir(repo_dir)
        .args(["fetch", "origin", sha, "--depth=1"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output();

    if let Ok(output) = fetch_result {
        if !output.status.success() {
            // Try fetching without specific SHA (might already have it)
            debug!("Specific SHA fetch failed, trying general fetch...");
            let _ = Command::new("git")
                .current_dir(repo_dir)
                .args(["fetch", "origin", "--depth=1"])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output();
        }
    }

    // Use git ls-tree to find all package.xml files without checkout
    let mut packages = discover_packages_from_git_objects(repo_dir, sha)?;

    // Handle submodules if requested
    if recurse_submodules {
        let submodule_packages = discover_packages_from_submodules(repo_dir, sha)?;
        packages.extend(submodule_packages);
    }

    debug!("Found {} packages via git object inspection", packages.len());
    Ok(packages)
}

/// Discover packages by reading git objects directly (no checkout needed)
fn discover_packages_from_git_objects(
    repo_dir: &Path,
    sha: &str,
) -> crate::Result<Vec<PackageInfo>> {
    // List all files at this SHA
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["ls-tree", "-r", "--name-only", sha])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git ls-tree: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git ls-tree failed for {}: {}",
            sha, stderr
        )));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut packages = Vec::new();

    // Find all package.xml files
    for line in stdout.lines() {
        if line.ends_with("/package.xml") || line == "package.xml" {
            // Read the file content via git show
            let file_ref = format!("{}:{}", sha, line);
            let content_output = Command::new("git")
                .current_dir(repo_dir)
                .args(["show", &file_ref])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output()
                .map_err(|e| crate::Error::Git(format!("failed to run git show: {e}")))?;

            if content_output.status.success() {
                let content = String::from_utf8_lossy(&content_output.stdout);
                let pkg_path = if let Some(parent) = Path::new(line).parent() {
                    let p = parent.to_string_lossy().to_string();
                    if p.is_empty() {
                        ".".to_string()
                    } else {
                        p
                    }
                } else {
                    ".".to_string()
                };

                match parse_package_xml(&content, &pkg_path) {
                    Ok(info) => {
                        debug!("Found package '{}' at {}", info.name, pkg_path);
                        packages.push(info);
                    }
                    Err(e) => {
                        warn!("Failed to parse {}: {}", line, e);
                    }
                }
            }
        }
    }

    Ok(packages)
}

/// Discover packages from submodules using git object inspection
fn discover_packages_from_submodules(
    repo_dir: &Path,
    sha: &str,
) -> crate::Result<Vec<PackageInfo>> {
    // Parse .gitmodules to find submodule paths
    let gitmodules_ref = format!("{}:.gitmodules", sha);
    let output = Command::new("git")
        .current_dir(repo_dir)
        .args(["show", &gitmodules_ref])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output();

    let Ok(output) = output else {
        return Ok(Vec::new());
    };

    if !output.status.success() {
        // No .gitmodules file, no submodules
        return Ok(Vec::new());
    }

    let gitmodules = String::from_utf8_lossy(&output.stdout);
    let mut packages = Vec::new();

    // Parse submodule paths from .gitmodules
    // Format: [submodule "name"]\n\tpath = <path>\n\turl = <url>
    let mut current_path: Option<String> = None;
    for line in gitmodules.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("path = ") {
            current_path = Some(trimmed.strip_prefix("path = ").unwrap().to_string());
        } else if trimmed.starts_with("[submodule") {
            current_path = None;
        }

        if let Some(ref path) = current_path {
            // Get the submodule commit SHA from the tree
            let output = Command::new("git")
                .current_dir(repo_dir)
                .args(["ls-tree", sha, path])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output();

            if let Ok(output) = output {
                if output.status.success() {
                    let stdout = String::from_utf8_lossy(&output.stdout);
                    // Format: "160000 commit <sha>\t<path>"
                    if let Some(line) = stdout.lines().next() {
                        let parts: Vec<&str> = line.split_whitespace().collect();
                        if parts.len() >= 3 && parts[1] == "commit" {
                            let submodule_sha = parts[2];
                            let submodule_dir = repo_dir.join(path);

                            // If submodule is initialized, inspect its objects
                            if submodule_dir.join(".git").exists() {
                                debug!(
                                    "Inspecting submodule at {} (SHA: {})",
                                    path,
                                    &submodule_sha[..8.min(submodule_sha.len())]
                                );

                                // Fetch the submodule SHA
                                let _ = Command::new("git")
                                    .current_dir(&submodule_dir)
                                    .args(["fetch", "origin", submodule_sha, "--depth=1"])
                                    .stdout(Stdio::piped())
                                    .stderr(Stdio::piped())
                                    .output();

                                if let Ok(mut sub_packages) =
                                    discover_packages_from_git_objects(&submodule_dir, submodule_sha)
                                {
                                    // Prefix paths with submodule path
                                    for pkg in &mut sub_packages {
                                        pkg.path = format!("{}/{}", path, pkg.path);
                                    }
                                    packages.extend(sub_packages);
                                }
                            }
                        }
                    }
                }
            }
            current_path = None;
        }
    }

    Ok(packages)
}

/// Blobless no-checkout clone of a repository into `repo_dir`.
///
/// Fetches tree and commit objects without file blobs, leaving no working tree on
/// disk.  Individual blobs (package.xml files) are fetched on demand by subsequent
/// `git show` calls in `discover_packages_from_existing_repo`.
pub fn blobless_clone(url: &str, repo_dir: &Path) -> crate::Result<()> {
    if let Some(parent) = repo_dir.parent() {
        std::fs::create_dir_all(parent).map_err(|e| {
            crate::Error::Git(format!(
                "failed to create directory {}: {e}",
                parent.display()
            ))
        })?;
    }

    debug!("Blobless clone {} into {}", url, repo_dir.display());

    let output = Command::new("git")
        .args([
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            url,
            repo_dir.to_str().unwrap(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run git clone: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "git clone failed for {url}: {stderr}"
        )));
    }

    Ok(())
}

/// Resolve a version (tag, branch, or SHA) to a concrete SHA.
///
/// Resolution strategy:
/// 1. If `version` is already a full 40-character SHA, return it directly.
/// 2. If a local clone exists at `repo_dir`, use `git fetch` + `FETCH_HEAD`.
/// 3. Otherwise, do a blobless clone, then use `git fetch` + `FETCH_HEAD`.
///
/// This replaces the old `ls-remote`-based resolution, which could not re-index
/// packages when a version changed (new packages may appear in a repo for a new
/// tag/branch).  Blobless clone is always required for package discovery anyway,
/// so driving version resolution through the local clone avoids a redundant
/// network round-trip.
fn resolve_version_from_repo(
    url: &str,
    version: &str,
    repo_dir: Option<&Path>,
) -> crate::Result<String> {
    // Short-circuit: full SHAs are already resolved.
    if version.len() == 40 && version.chars().all(|c| c.is_ascii_hexdigit()) {
        debug!("Version {} is a full SHA, using directly", version);
        return Ok(version.to_string());
    }

    let dir = repo_dir.ok_or_else(|| {
        crate::Error::Git(format!(
            "a source directory (--src) is required to resolve non-SHA version '{version}'"
        ))
    })?;

    // Ensure a local clone exists to resolve from.
    if !dir.join(".git").exists() {
        blobless_clone(url, dir)?;
    }

    resolve_version_local(dir, version)
}

/// Generate a lockfile from a .repos file
///
/// # Arguments
/// * `repos` - Parsed .repos file
/// * `src_dir` - Optional source directory for cloning repositories.
///   If Some, repos are cloned to `src_dir/workspace_path`.
///   If None, temporary directories are used.
/// * `recurse_submodules` - Whether to recursively clone submodules
///
/// # Returns
/// A complete lockfile with resolved SHAs and package information
pub fn generate_lockfile(
    repos: &ReposFile,
    src_dir: Option<&Path>,
    recurse_submodules: bool,
) -> crate::Result<Lockfile> {
    let mut lockfile = Lockfile::new();

    for (workspace_path, entry) in &repos.repositories {
        if entry.repo_type != "git" {
            warn!(
                "Skipping non-git repository at {}: type={}",
                workspace_path, entry.repo_type
            );
            continue;
        }

        // Determine repo directory
        let repo_dir = src_dir.map(|dir| dir.join(workspace_path));

        // Resolve version to SHA
        let sha = resolve_version_from_repo(&entry.url, &entry.version, repo_dir.as_deref())?;
        debug!("Resolved {} {} -> {}", workspace_path, entry.version, sha);

        // Discover packages in this repository
        let packages = discover_packages(&entry.url, &sha, repo_dir.as_deref(), recurse_submodules)?;

        // Add to repositories map (using workspace_path as key, same as .repos format)
        let mut package_names: Vec<String> = packages.iter().map(|p| p.name.clone()).collect();
        package_names.sort();

        // Only include ref if it's different from the resolved SHA
        // (omit if original version was already a full SHA - nothing to update)
        let version_ref = if entry.version != sha {
            Some(entry.version.clone())
        } else {
            None
        };

        lockfile.repositories.insert(
            workspace_path.clone(),
            RepoLock {
                repo_type: "git".to_string(),
                url: entry.url.clone(),
                version: sha, // Pinned SHA for VCS checkout
                version_ref,  // Original ref for update tracking (if different)
                packages: package_names,
            },
        );

        // Add packages to packages map
        for pkg in packages {
            // Check for duplicate package names
            if let Some(existing) = lockfile.packages.get(&pkg.name) {
                return Err(crate::Error::DuplicatePackage {
                    package: pkg.name,
                    repo1: existing.repo.clone(),
                    repo2: workspace_path.clone(),
                });
            }

            lockfile.packages.insert(
                pkg.name,
                PackageLock {
                    repo: workspace_path.clone(),
                    path: pkg.path,
                    dependencies: Dependencies::default(),
                },
            );
        }
    }

    Ok(lockfile)
}

/// Dependency resolution mode
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DependencyMode {
    /// Build dependencies only (for colcon build)
    Build,
    /// Execution dependencies only (for runtime)
    Exec,
    /// Both build and exec dependencies
    BuildAndExec,
    /// All dependencies including test
    All,
}

/// Result of transitive dependency resolution
#[derive(Debug, Clone, Default)]
pub struct DependencyGraph {
    /// All packages needed (root + transitive)
    pub packages: HashSet<String>,

    /// Packages that were requested but not found in lockfile
    pub missing: HashSet<String>,

    /// Packages that are external (not in lockfile, e.g., system deps)
    pub external: HashSet<String>,
}

impl DependencyGraph {
    fn new() -> Self {
        Self::default()
    }
}

/// Read on-disk `package.xml` dependencies for a package.
///
/// Returns `None` if the package is not in the lockfile or its `package.xml` is not on disk.
fn read_package_deps(
    lockfile: &Lockfile,
    src_dir: &std::path::Path,
    pkg_name: &str,
) -> Option<Dependencies> {
    let pkg_lock = lockfile.packages.get(pkg_name)?;
    let xml_path = src_dir.join(&pkg_lock.repo).join(&pkg_lock.path).join("package.xml");
    let content = std::fs::read_to_string(&xml_path).ok()?;
    let parsed = parse_package_xml(&content, &xml_path.to_string_lossy()).ok()?;
    Some(parsed.dependencies)
}

/// Compute transitive dependencies for a set of root packages.
///
/// Reads dependency information from on-disk `package.xml` files, not from
/// the lockfile.  The lockfile is used only to map package names to repo/path.
/// Packages whose `package.xml` is not on disk are treated as leaves — their
/// transitive deps are unknown until fetched.
///
/// # Arguments
/// * `lockfile` - Lockfile for package-name → repo/path mapping
/// * `src_dir`  - Source directory containing fetched repositories
/// * `root_packages` - Initial set of packages to resolve
/// * `mode` - Which dependency types to include
pub fn resolve_dependencies(
    lockfile: &Lockfile,
    src_dir: &std::path::Path,
    root_packages: &HashSet<String>,
    mode: DependencyMode,
) -> DependencyGraph {
    let mut result = DependencyGraph::new();
    let mut queue: Vec<String> = root_packages.iter().cloned().collect();
    let mut visited: HashSet<String> = HashSet::new();

    while let Some(pkg_name) = queue.pop() {
        if visited.contains(&pkg_name) {
            continue;
        }
        visited.insert(pkg_name.clone());

        // Look up package in lockfile (for repo/path mapping)
        if !lockfile.packages.contains_key(&pkg_name) {
            result.external.insert(pkg_name);
            continue;
        }

        result.packages.insert(pkg_name.clone());

        // Read deps from on-disk package.xml; skip if not fetched yet.
        let Some(deps) = read_package_deps(lockfile, src_dir, &pkg_name) else {
            result.missing.insert(pkg_name);
            continue;
        };

        match mode {
            DependencyMode::Build => {
                queue.extend(deps.build.iter().cloned());
                queue.extend(deps.build_export.iter().cloned());
                queue.extend(deps.buildtool.iter().cloned());
                queue.extend(deps.buildtool_export.iter().cloned());
            }
            DependencyMode::Exec => {
                queue.extend(deps.exec.iter().cloned());
            }
            DependencyMode::BuildAndExec => {
                queue.extend(deps.build.iter().cloned());
                queue.extend(deps.build_export.iter().cloned());
                queue.extend(deps.buildtool.iter().cloned());
                queue.extend(deps.buildtool_export.iter().cloned());
                queue.extend(deps.exec.iter().cloned());
            }
            DependencyMode::All => {
                queue.extend(deps.build.iter().cloned());
                queue.extend(deps.build_export.iter().cloned());
                queue.extend(deps.buildtool.iter().cloned());
                queue.extend(deps.buildtool_export.iter().cloned());
                queue.extend(deps.exec.iter().cloned());
                queue.extend(deps.test.iter().cloned());
            }
        }
    }

    result
}

/// Compute build order for packages using topological sort.
///
/// Reads dependency information from on-disk `package.xml` files.
/// Returns packages in dependency order (dependencies first).
/// Packages with circular dependencies are placed at the end.
pub fn compute_build_order(
    lockfile: &Lockfile,
    src_dir: &std::path::Path,
    packages: &HashSet<String>,
) -> Vec<String> {
    let mut in_degree: HashMap<String, usize> = HashMap::new();
    let mut dependents: HashMap<String, Vec<String>> = HashMap::new();

    for pkg in packages {
        in_degree.insert(pkg.clone(), 0);
        dependents.insert(pkg.clone(), Vec::new());
    }

    // Count incoming edges from on-disk package.xml build deps.
    for pkg in packages {
        if let Some(deps) = read_package_deps(lockfile, src_dir, pkg) {
            let build_deps = deps.build.iter()
                .chain(deps.build_export.iter())
                .chain(deps.buildtool.iter())
                .chain(deps.buildtool_export.iter());
            for dep in build_deps {
                if packages.contains(dep) {
                    *in_degree.get_mut(pkg).unwrap() += 1;
                    dependents.get_mut(dep).unwrap().push(pkg.clone());
                }
            }
        }
    }

    // Kahn's algorithm for topological sort
    let mut result = Vec::new();
    let mut queue: Vec<String> = in_degree
        .iter()
        .filter(|(_, deg)| **deg == 0)
        .map(|(pkg, _)| pkg.clone())
        .collect();
    queue.sort();

    while let Some(pkg) = queue.pop() {
        result.push(pkg.clone());

        if let Some(deps) = dependents.get(&pkg) {
            for dep in deps {
                let degree = in_degree.get_mut(dep).unwrap();
                *degree -= 1;
                if *degree == 0 {
                    queue.push(dep.clone());
                    queue.sort();
                }
            }
        }
    }

    // Add any remaining packages (cycles) at the end
    for pkg in packages {
        if !result.contains(pkg) {
            result.push(pkg.clone());
        }
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_repos() {
        let content = r"
repositories:
  core/autoware_msgs:
    type: git
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 1.11.0
";
        let repos = parse_repos(content).unwrap();
        assert_eq!(repos.repositories.len(), 1);

        let entry = repos.repositories.get("core/autoware_msgs").unwrap();
        assert_eq!(entry.repo_type, "git");
        assert_eq!(entry.version, "1.11.0");
    }

    #[test]
    fn test_parse_repos_multiple() {
        let content = r"
repositories:
  core/autoware_msgs:
    type: git
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 1.11.0
  universe/autoware_utils:
    type: git
    url: https://github.com/autowarefoundation/autoware_utils.git
    version: main
";
        let repos = parse_repos(content).unwrap();
        assert_eq!(repos.repositories.len(), 2);
        assert!(repos.repositories.contains_key("core/autoware_msgs"));
        assert!(repos.repositories.contains_key("universe/autoware_utils"));
    }

    #[test]
    fn test_lockfile_roundtrip() {
        let mut lockfile = Lockfile::new();
        lockfile.repositories.insert(
            "core/autoware_msgs".to_string(),
            RepoLock {
                repo_type: "git".to_string(),
                url: "https://github.com/autowarefoundation/autoware_msgs.git".to_string(),
                version: "abc123def456".to_string(),     // SHA
                version_ref: Some("1.11.0".to_string()), // Original tag
                packages: vec!["autoware_msgs".to_string()],
            },
        );
        lockfile.packages.insert(
            "autoware_msgs".to_string(),
            PackageLock {
                repo: "core/autoware_msgs".to_string(),
                path: "autoware_msgs".to_string(),
                dependencies: Dependencies::default(),
            },
        );

        let yaml = serialize_lockfile(&lockfile).unwrap();
        let parsed = parse_lockfile(&yaml).unwrap();

        assert_eq!(parsed.repositories.len(), 1);
        assert_eq!(parsed.packages.len(), 1);
        // Verify VCS-compatible fields
        let repo = parsed.repositories.get("core/autoware_msgs").unwrap();
        assert_eq!(repo.repo_type, "git");
        assert_eq!(repo.version, "abc123def456");
    }

    #[test]
    fn test_remove_repo() {
        let mut lockfile = Lockfile::new();

        // Add two repos with packages
        lockfile.repositories.insert(
            "core/msgs".to_string(),
            RepoLock {
                repo_type: "git".to_string(),
                url: "https://example.com/msgs.git".to_string(),
                version: "abc123".to_string(),
                version_ref: Some("1.0.0".to_string()),
                packages: vec!["pkg_a".to_string(), "pkg_b".to_string()],
            },
        );
        lockfile.repositories.insert(
            "universe/utils".to_string(),
            RepoLock {
                repo_type: "git".to_string(),
                url: "https://example.com/utils.git".to_string(),
                version: "def456".to_string(),
                version_ref: Some("2.0.0".to_string()),
                packages: vec!["pkg_c".to_string()],
            },
        );

        lockfile.packages.insert(
            "pkg_a".to_string(),
            PackageLock {
                repo: "core/msgs".to_string(),
                path: "pkg_a".to_string(),
                dependencies: Dependencies::default(),
            },
        );
        lockfile.packages.insert(
            "pkg_b".to_string(),
            PackageLock {
                repo: "core/msgs".to_string(),
                path: "pkg_b".to_string(),
                dependencies: Dependencies::default(),
            },
        );
        lockfile.packages.insert(
            "pkg_c".to_string(),
            PackageLock {
                repo: "universe/utils".to_string(),
                path: "pkg_c".to_string(),
                dependencies: Dependencies::default(),
            },
        );

        assert_eq!(lockfile.repositories.len(), 2);
        assert_eq!(lockfile.packages.len(), 3);

        // Remove core/msgs - should also remove pkg_a and pkg_b
        lockfile.remove_repo("core/msgs");

        assert_eq!(lockfile.repositories.len(), 1);
        assert_eq!(lockfile.packages.len(), 1);
        assert!(!lockfile.repositories.contains_key("core/msgs"));
        assert!(lockfile.repositories.contains_key("universe/utils"));
        assert!(!lockfile.packages.contains_key("pkg_a"));
        assert!(!lockfile.packages.contains_key("pkg_b"));
        assert!(lockfile.packages.contains_key("pkg_c"));
    }

    #[test]
    fn test_parse_package_xml_basic() {
        let content = r#"<?xml version="1.0"?>
<package format="3">
  <name>my_package</name>
  <version>1.0.0</version>
  <description>A test package</description>
  <maintainer email="test@example.com">Test</maintainer>
  <license>Apache-2.0</license>

  <buildtool_depend>ament_cmake</buildtool_depend>
  <build_depend>rclcpp</build_depend>
  <exec_depend>std_msgs</exec_depend>
  <test_depend>ament_lint_auto</test_depend>
</package>
"#;
        let info = parse_package_xml(content, "my_package").unwrap();
        assert_eq!(info.name, "my_package");
        assert_eq!(info.path, "my_package");
        assert!(info.dependencies.buildtool.contains(&"ament_cmake".to_string()));
        assert!(info.dependencies.build.contains(&"rclcpp".to_string()));
        assert!(info.dependencies.exec.contains(&"std_msgs".to_string()));
        assert!(
            info.dependencies
                .test
                .contains(&"ament_lint_auto".to_string())
        );
    }

    #[test]
    fn test_parse_package_xml_depend() {
        // Test <depend> which adds to both build and exec
        let content = r#"<?xml version="1.0"?>
<package format="3">
  <name>sensor_pkg</name>
  <depend>sensor_msgs</depend>
  <depend>geometry_msgs</depend>
</package>
"#;
        let info = parse_package_xml(content, "sensor_pkg").unwrap();
        assert_eq!(info.name, "sensor_pkg");
        // <depend> should add to both build and exec
        assert!(info.dependencies.build.contains(&"sensor_msgs".to_string()));
        assert!(
            info.dependencies
                .build
                .contains(&"geometry_msgs".to_string())
        );
        assert!(info.dependencies.exec.contains(&"sensor_msgs".to_string()));
        assert!(
            info.dependencies
                .exec
                .contains(&"geometry_msgs".to_string())
        );
    }

    #[test]
    fn test_parse_package_xml_missing_name() {
        let content = r#"<?xml version="1.0"?>
<package format="3">
  <version>1.0.0</version>
</package>
"#;
        let result = parse_package_xml(content, "test");
        assert!(result.is_err());
    }

}
