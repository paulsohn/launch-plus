//! Package and launch file locator
//!
//! This module provides functionality to locate packages and launch files
//! using the `<package> <launcher>` convention (like `ros2 launch`).
//!
//! # Two Resolution Modes
//!
//! ## 1. Lockfile Mode (`resolve_*` methods)
//! - **Deterministic**: Computes paths from lockfile without checking disk
//! - **Fetch-on-demand**: Caller fetches if file doesn't exist
//! - **Primary mode for launch-plus**: Lockfile is source of truth
//!
//! ## 2. Discovery Mode (`locate_*` methods)
//! - **Filesystem search**: Searches disk for existing files
//! - **Fallback**: Uses AMENT_PREFIX_PATH for installed packages
//! - **Development mode**: Works without lockfile
//!
//! # Launch File Convention
//!
//! Launch files are expected in `<package_path>/launch/<launcher>`.
//! This matches the standard ROS 2 package layout.
//!
//! # Usage
//!
//! ```ignore
//! // Lockfile mode (recommended for launch-plus)
//! let locator = locator_from_lockfile(workspace, lockfile);
//! let path = locator.resolve_launch_file("autoware_launch", "autoware.launch.xml")?;
//! if !path.exists() {
//!     fetcher.sparse_checkout(&path)?;
//! }
//!
//! // Discovery mode (development/testing)
//! let locator = locator_from_workspace(workspace);
//! let path = locator.locate_launch_file("my_pkg", "test.launch.xml")?;
//! ```

use crate::indexer::{Lockfile, PackageLock};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// Package locator that resolves package names to file system paths
#[derive(Debug, Clone)]
pub struct PackageLocator {
    /// Path to workspace src directory (where packages are cloned)
    workspace_src: Option<PathBuf>,

    /// Lockfile for package path lookups
    lockfile: Option<Lockfile>,

    /// Additional search paths (e.g., from AMENT_PREFIX_PATH)
    ament_prefixes: Vec<PathBuf>,
}

impl Default for PackageLocator {
    fn default() -> Self {
        Self::new()
    }
}

impl PackageLocator {
    /// Create a new empty locator
    #[must_use]
    pub fn new() -> Self {
        Self {
            workspace_src: None,
            lockfile: None,
            ament_prefixes: Vec::new(),
        }
    }

    /// Create a locator with workspace path
    #[must_use]
    pub fn with_workspace<P: AsRef<Path>>(workspace_src: P) -> Self {
        Self {
            workspace_src: Some(workspace_src.as_ref().to_path_buf()),
            lockfile: None,
            ament_prefixes: Vec::new(),
        }
    }

    /// Set the workspace src directory
    pub fn set_workspace<P: AsRef<Path>>(&mut self, workspace_src: P) -> &mut Self {
        self.workspace_src = Some(workspace_src.as_ref().to_path_buf());
        self
    }

    /// Set the lockfile for package lookups
    pub fn set_lockfile(&mut self, lockfile: Lockfile) -> &mut Self {
        self.lockfile = Some(lockfile);
        self
    }

    /// Add AMENT_PREFIX_PATH from environment
    pub fn add_ament_from_env(&mut self) -> &mut Self {
        if let Ok(ament_path) = std::env::var("AMENT_PREFIX_PATH") {
            for prefix in ament_path.split(':') {
                if !prefix.is_empty() {
                    self.ament_prefixes.push(PathBuf::from(prefix));
                }
            }
        }
        self
    }

    /// Add a single ament prefix path
    pub fn add_ament_prefix<P: AsRef<Path>>(&mut self, prefix: P) -> &mut Self {
        self.ament_prefixes.push(prefix.as_ref().to_path_buf());
        self
    }

    // =========================================================================
    // Lockfile Mode: Deterministic path resolution (no existence check)
    // =========================================================================

    /// Get package lock info from lockfile
    ///
    /// Returns the package's lockfile entry if available.
    pub fn get_package_lock(&self, package: &str) -> Option<&PackageLock> {
        self.lockfile.as_ref()?.packages.get(package)
    }

    /// Check if a package exists in the lockfile
    pub fn has_package(&self, package: &str) -> bool {
        self.get_package_lock(package).is_some()
    }

    /// Resolve package share path from lockfile (no existence check)
    ///
    /// Computes the expected path based on lockfile information.
    /// The caller is responsible for ensuring the file exists (via sparse-checkout).
    ///
    /// Returns `Some(path)` if package is in lockfile, `None` otherwise.
    pub fn resolve_package_share(&self, package: &str) -> Option<PathBuf> {
        let lockfile = self.lockfile.as_ref()?;
        let workspace_src = self.workspace_src.as_ref()?;
        let pkg_lock = lockfile.packages.get(package)?;

        Some(workspace_src.join(&pkg_lock.repo).join(&pkg_lock.path))
    }

    /// Resolve launch file path from lockfile (no existence check)
    ///
    /// Computes: `<workspace>/<repo>/<pkg_path>/launch/<launcher>`
    ///
    /// The caller should sparse-checkout if the file doesn't exist.
    pub fn resolve_launch_file(&self, package: &str, launcher: &str) -> Option<PathBuf> {
        let pkg_share = self.resolve_package_share(package)?;
        Some(pkg_share.join("launch").join(launcher))
    }

    /// Resolve any file under package share from lockfile (no existence check)
    ///
    /// Computes: `<workspace>/<repo>/<pkg_path>/<share_path>`
    ///
    /// This is used for FileDependency resolution where share_path
    /// already includes the subdirectory (e.g., "launch/foo.xml", "config/params.yaml").
    pub fn resolve_share_file(&self, package: &str, share_path: &Path) -> Option<PathBuf> {
        let pkg_share = self.resolve_package_share(package)?;
        Some(pkg_share.join(share_path))
    }

    /// Resolve a file from AMENT_PREFIX_PATH (install mode).
    ///
    /// Returns the first `<ament_prefix>/share/<package>/<share_path>` that exists.
    /// Returns `None` if the file is not found in any configured AMENT_PREFIX_PATH entry.
    pub fn resolve_install_file(&self, package: &str, share_path: &Path) -> Option<PathBuf> {
        for prefix in &self.ament_prefixes {
            let path = prefix.join("share").join(package).join(share_path);
            if path.exists() {
                return Some(path);
            }
        }
        None
    }

    /// Locate a package's share directory from AMENT_PREFIX_PATH only.
    ///
    /// Unlike [`locate_package_share`] this skips the workspace entirely and
    /// only returns paths from the colcon install tree.  Used in non-preview
    /// mode where the resolved output must reference installed artifacts.
    pub fn locate_install_share(&self, package: &str) -> Option<PathBuf> {
        for prefix in &self.ament_prefixes {
            let share_path = prefix.join("share").join(package);
            if share_path.exists() {
                return Some(share_path);
            }
        }
        None
    }

    /// Resolve launch file path, returning error with details if package not in lockfile
    pub fn resolve_launch_file_or_err(
        &self,
        package: &str,
        launcher: &str,
    ) -> crate::Result<PathBuf> {
        self.resolve_launch_file(package, launcher).ok_or_else(|| {
            if self.lockfile.is_none() {
                crate::Error::LaunchParse("no lockfile configured in locator".to_string())
            } else if self.workspace_src.is_none() {
                crate::Error::LaunchParse("no workspace configured in locator".to_string())
            } else {
                crate::Error::LaunchParse(format!("package '{}' not found in lockfile", package))
            }
        })
    }

    // =========================================================================
    // Discovery Mode: Filesystem search (checks existence)
    // =========================================================================

    /// Locate a package's share directory (discovery mode)
    ///
    /// Searches filesystem for an existing package directory.
    /// Use `resolve_package_share` for lockfile-based resolution.
    ///
    /// Search order:
    /// 1. Lockfile + workspace (if configured and exists on disk)
    /// 2. Direct workspace scan (common layouts)
    /// 3. AMENT_PREFIX_PATH (installed packages)
    pub fn locate_package_share(&self, package: &str) -> Option<PathBuf> {
        // Strategy 1: Use lockfile + workspace
        if let (Some(lockfile), Some(workspace_src)) = (&self.lockfile, &self.workspace_src) {
            if let Some(pkg_lock) = lockfile.packages.get(package) {
                // Construct: <workspace_src>/<repo>/<path>
                let pkg_path = workspace_src.join(&pkg_lock.repo).join(&pkg_lock.path);
                if pkg_path.exists() {
                    return Some(pkg_path);
                }
            }
        }

        // Strategy 2: Direct workspace scan (common package layouts)
        if let Some(workspace_src) = &self.workspace_src {
            // Try: <workspace>/<package>/<package> (ROS 2 Python package layout)
            let python_layout = workspace_src.join(package).join(package);
            if python_layout.exists() {
                return Some(python_layout);
            }

            // Try: <workspace>/<package> (simple layout)
            let simple_layout = workspace_src.join(package);
            if simple_layout.exists() && simple_layout.join("package.xml").exists() {
                return Some(simple_layout);
            }

            // Recursive search for package directory
            if let Some(found) = self.search_package_in_workspace(workspace_src, package) {
                return Some(found);
            }
        }

        // Strategy 3: AMENT_PREFIX_PATH (installed packages)
        for prefix in &self.ament_prefixes {
            let share_path = prefix.join("share").join(package);
            if share_path.exists() {
                return Some(share_path);
            }
        }

        None
    }

    /// Search for a package directory recursively in the workspace
    fn search_package_in_workspace(&self, workspace: &Path, package: &str) -> Option<PathBuf> {
        let entries = match std::fs::read_dir(workspace) {
            Ok(e) => e,
            Err(e) => {
                tracing::debug!("cannot read workspace dir {}: {e}", workspace.display());
                return None;
            }
        };

        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }

            // Check if this directory contains the package
            // Pattern: <repo>/<package> or <repo>/<subdir>/<package>
            let nested = path.join(package);
            if nested.exists() && nested.join("package.xml").exists() {
                return Some(nested);
            }

            // Check Python package layout: <repo>/<package>/<package>
            let python_nested = nested.join(package);
            if python_nested.exists() {
                return Some(python_nested);
            }

            // Recurse one level deeper (common in monorepos)
            if let Ok(sub_entries) = std::fs::read_dir(&path) {
                for sub_entry in sub_entries.flatten() {
                    let sub_path = sub_entry.path();
                    if sub_path.is_dir() {
                        let deep_nested = sub_path.join(package);
                        if deep_nested.exists() && deep_nested.join("package.xml").exists() {
                            return Some(deep_nested);
                        }
                    }
                }
            }
        }

        None
    }

    /// Locate a launch file (discovery mode)
    ///
    /// Searches filesystem for an existing launch file.
    /// Use `resolve_launch_file` for lockfile-based resolution.
    ///
    /// Launch files are expected at `<package_share>/launch/<launcher>`.
    ///
    /// # Arguments
    /// * `package` - Package name (e.g., "autoware_launch")
    /// * `launcher` - Launch file name (e.g., "autoware.launch.xml")
    pub fn locate_launch_file(&self, package: &str, launcher: &str) -> Option<PathBuf> {
        let pkg_share = self.locate_package_share(package)?;
        let launch_path = pkg_share.join("launch").join(launcher);

        if launch_path.exists() {
            Some(launch_path)
        } else {
            None
        }
    }

    /// Return all known package share paths (workspace + AMENT_PREFIX_PATH).
    ///
    /// Maps package name → absolute path to the package root in the workspace.
    /// Used in **preview mode** to pass workspace package locations to external tools
    /// (e.g. py_resolver.py) so they can resolve `$(find-pkg-share X)` substitutions
    /// against source packages.
    ///
    /// In non-preview mode use [`all_install_shares`] instead.
    pub fn all_package_shares(&self) -> HashMap<String, String> {
        let mut result = HashMap::new();
        if let (Some(lockfile), Some(workspace)) = (&self.lockfile, &self.workspace_src) {
            for (pkg_name, pkg_lock) in &lockfile.packages {
                let share_path = workspace.join(&pkg_lock.repo).join(&pkg_lock.path);
                result.insert(pkg_name.clone(), share_path.to_string_lossy().into_owned());
            }
        }
        // AMENT_PREFIX_PATH: share/<package_name>/ mirrors the source layout
        for prefix in &self.ament_prefixes {
            let share_root = prefix.join("share");
            if let Ok(entries) = std::fs::read_dir(&share_root) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.is_dir() {
                        let pkg_name = entry.file_name().to_string_lossy().into_owned();
                        result
                            .entry(pkg_name)
                            .or_insert_with(|| path.to_string_lossy().into_owned());
                    }
                }
            }
        }
        result
    }

    /// Return package share paths from AMENT_PREFIX_PATH only.
    ///
    /// Maps package name → `<ament_prefix>/share/<package>`.
    /// Used in **non-preview mode** where the resolved output must reference
    /// installed artifacts, not workspace source paths.
    pub fn all_install_shares(&self) -> HashMap<String, String> {
        let mut result = HashMap::new();
        for prefix in &self.ament_prefixes {
            let share_root = prefix.join("share");
            if let Ok(entries) = std::fs::read_dir(&share_root) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.is_dir() {
                        let pkg_name = entry.file_name().to_string_lossy().into_owned();
                        result
                            .entry(pkg_name)
                            .or_insert_with(|| path.to_string_lossy().into_owned());
                    }
                }
            }
        }
        result
    }

    /// Locate a launch file (discovery mode), returning error if not found
    pub fn locate_launch_file_or_err(
        &self,
        package: &str,
        launcher: &str,
    ) -> crate::Result<PathBuf> {
        self.locate_launch_file(package, launcher).ok_or_else(|| {
            crate::Error::LaunchParse(format!(
                "launch file not found: {}/launch/{}",
                package, launcher
            ))
        })
    }
}

/// Convenience function to create a locator from workspace path
#[must_use]
pub fn locator_from_workspace<P: AsRef<Path>>(workspace_src: P) -> PackageLocator {
    let mut locator = PackageLocator::with_workspace(workspace_src);
    locator.add_ament_from_env();
    locator
}

/// Convenience function to create a locator from workspace and lockfile
#[must_use]
pub fn locator_from_lockfile<P: AsRef<Path>>(
    workspace_src: P,
    lockfile: Lockfile,
) -> PackageLocator {
    let mut locator = PackageLocator::with_workspace(workspace_src);
    locator.set_lockfile(lockfile);
    locator.add_ament_from_env();
    locator
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::indexer::{Dependencies, PackageLock, RepoLock};
    use std::fs;
    use std::path::PathBuf;
    use tempfile::TempDir;

    /// Create a mock lockfile for testing
    fn mock_lockfile() -> Lockfile {
        let mut lockfile = Lockfile::new();

        // Add a repository
        lockfile.repositories.insert(
            "autoware/launcher/autoware_launch".to_string(),
            RepoLock {
                repo_type: "git".to_string(),
                url: "https://github.com/autowarefoundation/autoware_launch.git".to_string(),
                version: "abc123".to_string(),
                version_ref: Some("main".to_string()),
                packages: vec!["autoware_launch".to_string()],
            },
        );

        // Add a package
        lockfile.packages.insert(
            "autoware_launch".to_string(),
            PackageLock {
                repo: "autoware/launcher/autoware_launch".to_string(),
                path: "autoware_launch".to_string(),
                dependencies: Dependencies::default(),
            },
        );

        lockfile
    }

    // =========================================================================
    // Lockfile mode tests (resolve_*)
    // =========================================================================

    #[test]
    fn test_resolve_package_share() {
        let temp = TempDir::new().unwrap();
        let lockfile = mock_lockfile();
        let locator = locator_from_lockfile(temp.path(), lockfile);

        let result = locator.resolve_package_share("autoware_launch");

        assert!(result.is_some());
        // Path is computed from lockfile, doesn't check existence
        let expected = temp
            .path()
            .join("autoware/launcher/autoware_launch")
            .join("autoware_launch");
        assert_eq!(result.unwrap(), expected);
    }

    #[test]
    fn test_resolve_package_share_not_in_lockfile() {
        let temp = TempDir::new().unwrap();
        let lockfile = mock_lockfile();
        let locator = locator_from_lockfile(temp.path(), lockfile);

        let result = locator.resolve_package_share("nonexistent_pkg");
        assert!(result.is_none());
    }

    #[test]
    fn test_resolve_launch_file() {
        let temp = TempDir::new().unwrap();
        let lockfile = mock_lockfile();
        let locator = locator_from_lockfile(temp.path(), lockfile);

        let result = locator.resolve_launch_file("autoware_launch", "autoware.launch.xml");

        assert!(result.is_some());
        let expected = temp
            .path()
            .join("autoware/launcher/autoware_launch")
            .join("autoware_launch")
            .join("launch")
            .join("autoware.launch.xml");
        assert_eq!(result.unwrap(), expected);
    }

    #[test]
    fn test_resolve_share_file() {
        let temp = TempDir::new().unwrap();
        let lockfile = mock_lockfile();
        let locator = locator_from_lockfile(temp.path(), lockfile);

        // Simulates FileDependency with share_path
        let share_path = PathBuf::from("config/vehicle.yaml");
        let result = locator.resolve_share_file("autoware_launch", &share_path);

        assert!(result.is_some());
        let expected = temp
            .path()
            .join("autoware/launcher/autoware_launch")
            .join("autoware_launch")
            .join("config")
            .join("vehicle.yaml");
        assert_eq!(result.unwrap(), expected);
    }

    #[test]
    fn test_has_package() {
        let temp = TempDir::new().unwrap();
        let lockfile = mock_lockfile();
        let locator = locator_from_lockfile(temp.path(), lockfile);

        assert!(locator.has_package("autoware_launch"));
        assert!(!locator.has_package("nonexistent"));
    }

    #[test]
    fn test_get_package_lock() {
        let temp = TempDir::new().unwrap();
        let lockfile = mock_lockfile();
        let locator = locator_from_lockfile(temp.path(), lockfile);

        let pkg_lock = locator.get_package_lock("autoware_launch");
        assert!(pkg_lock.is_some());
        assert_eq!(pkg_lock.unwrap().path, "autoware_launch");
    }

    // =========================================================================
    // Discovery mode tests (locate_*)
    // =========================================================================

    #[test]
    fn test_locate_package_simple_layout() {
        let temp = TempDir::new().unwrap();
        let pkg_dir = temp.path().join("my_pkg");
        fs::create_dir_all(&pkg_dir).unwrap();
        fs::write(pkg_dir.join("package.xml"), "<package></package>").unwrap();

        let locator = PackageLocator::with_workspace(temp.path());
        let result = locator.locate_package_share("my_pkg");

        assert!(result.is_some());
        assert_eq!(result.unwrap(), pkg_dir);
    }

    #[test]
    fn test_locate_package_python_layout() {
        let temp = TempDir::new().unwrap();
        // Python package layout: my_pkg/my_pkg/
        let pkg_dir = temp.path().join("my_pkg").join("my_pkg");
        fs::create_dir_all(&pkg_dir).unwrap();

        let locator = PackageLocator::with_workspace(temp.path());
        let result = locator.locate_package_share("my_pkg");

        assert!(result.is_some());
        assert_eq!(result.unwrap(), pkg_dir);
    }

    #[test]
    fn test_locate_launch_file() {
        let temp = TempDir::new().unwrap();
        let pkg_dir = temp.path().join("my_pkg");
        let launch_dir = pkg_dir.join("launch");
        fs::create_dir_all(&launch_dir).unwrap();
        fs::write(pkg_dir.join("package.xml"), "<package></package>").unwrap();
        fs::write(launch_dir.join("test.launch.xml"), "<launch></launch>").unwrap();

        let locator = PackageLocator::with_workspace(temp.path());
        let result = locator.locate_launch_file("my_pkg", "test.launch.xml");

        assert!(result.is_some());
        assert_eq!(result.unwrap(), launch_dir.join("test.launch.xml"));
    }

    #[test]
    fn test_locate_launch_file_not_found() {
        let temp = TempDir::new().unwrap();

        let locator = PackageLocator::with_workspace(temp.path());
        let result = locator.locate_launch_file("nonexistent", "test.launch.xml");

        assert!(result.is_none());
    }
}
