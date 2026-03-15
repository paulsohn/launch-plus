//! Shared helpers for invoking `rosdep` from both the resolver and builder.
//!
//! Uses `rosdep resolve` to map rosdep keys → system package names, then
//! installs via `apt-get`/`pip` directly.
//!
//! **Why not `rosdep install`?**  Without `--from-paths`, `rosdep install`
//! treats arguments as ROS **package names** (via `rospkg.expand_to_packages`)
//! and fails on pure system keys like `asio` with `ResourceNotFound`.
//! With `--from-paths`, it scans every `package.xml` under the source tree and
//! pulls in unresolvable test-only dependencies.  Neither mode works for our
//! use case of installing an explicit set of rosdep keys extracted from the
//! dependency graph.
//!
//! Only `#apt` and `#pip` installers are supported.  Keys that resolve to
//! other installers (e.g. `#brew`) are treated as unresolved.

use std::collections::{BTreeSet, HashSet};
use std::process::Command;
use std::sync::{Once, OnceLock};

use tracing::info;

static ROSDEP_UPDATE: Once = Once::new();
static INSTALLED_PACKAGES: OnceLock<HashSet<String>> = OnceLock::new();

/// Run `rosdep update` once per process (best-effort, non-fatal).
pub fn ensure_rosdep_updated() {
    ROSDEP_UPDATE.call_once(|| {
        info!("Running one-time rosdep update");
        let _ = Command::new("rosdep").args(["update"]).status();
    });
}

/// Return the value of `$ROS_DISTRO`, or `None` if unset/empty.
fn ros_distro() -> Option<String> {
    std::env::var("ROS_DISTRO").ok().filter(|d| !d.is_empty())
}

/// System packages grouped by installer.
#[derive(Debug, Default)]
pub struct ResolvedDeps {
    /// Packages to install via `apt-get install`.
    pub apt: Vec<String>,
    /// Packages to install via `pip install`.
    pub pip: Vec<String>,
    /// Rosdep keys that could not be resolved.
    pub unresolved: Vec<String>,
}

/// Resolve rosdep keys to system package names via `rosdep resolve`.
///
/// Calls [`ensure_rosdep_updated`] first.  The output is parsed into
/// installer-grouped buckets.
///
/// # Output format
///
/// Single key:
/// ```text
/// #apt
/// ros-jazzy-rclcpp
/// ```
///
/// Multiple keys:
/// ```text
/// #ROSDEP[rclcpp]
/// #apt
/// ros-jazzy-rclcpp
/// #ROSDEP[eigen]
/// #apt
/// libeigen3-dev
/// ```
///
/// Keys with no mapping produce a bare `#ROSDEP[key]` header with no
/// installer line.  `rosdep resolve` exits non-zero when any key is
/// unresolvable (both humble and jazzy), but stdout still contains
/// valid resolutions for the keys that did resolve.
pub fn rosdep_resolve(keys: &[&str]) -> crate::Result<ResolvedDeps> {
    if keys.is_empty() {
        return Ok(ResolvedDeps::default());
    }

    let distro = ros_distro().ok_or_else(|| {
        crate::Error::ProcessExecution(
            "ROS_DISTRO is not set; cannot resolve rosdep keys".to_string(),
        )
    })?;

    ensure_rosdep_updated();

    let mut args = vec!["resolve", "--rosdistro", &distro];
    args.extend(keys.iter().copied());

    let output = Command::new("rosdep").args(&args).output().map_err(|e| {
        crate::Error::ProcessExecution(format!("failed to run rosdep resolve: {e}"))
    })?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut resolved = parse_rosdep_resolve(&stdout, keys)?;

    // Non-zero exit means at least one key was unresolvable.  Parse stderr
    // for the specific key names (format: "ERROR: no rosdep rule for 'foo'")
    // and merge them into the unresolved list.
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        for line in stderr.lines() {
            // Match: ERROR: no rosdep rule for 'KEY'
            if let Some(rest) = line.strip_prefix("ERROR: no rosdep rule for '") {
                if let Some(key) = rest.strip_suffix('\'') {
                    if !resolved.unresolved.contains(&key.to_string()) {
                        resolved.unresolved.push(key.to_string());
                    }
                }
            }
        }
    }

    Ok(resolved)
}

/// Parse the stdout of `rosdep resolve`.
fn parse_rosdep_resolve(stdout: &str, keys: &[&str]) -> crate::Result<ResolvedDeps> {
    let mut result = ResolvedDeps::default();
    let lines: Vec<&str> = stdout.lines().collect();

    if lines.is_empty() {
        result.unresolved = keys.iter().map(|s| s.to_string()).collect();
        return Ok(result);
    }

    // Single-key format: no #ROSDEP[...] headers
    if keys.len() == 1 && !lines[0].starts_with("#ROSDEP[") {
        let mut installer: Option<&str> = None;
        let mut has_supported = false;
        for line in &lines {
            if let Some(inst) = line.strip_prefix('#') {
                installer = Some(inst);
                if is_supported_installer(inst) {
                    has_supported = true;
                }
            } else if let Some(inst) = installer {
                collect_packages(&mut result, inst, line);
            }
        }
        if !has_supported {
            result.unresolved.push(keys[0].to_string());
        }
        return Ok(result);
    }

    // Multi-key format: #ROSDEP[key] headers
    let mut seen_keys: HashSet<String> = HashSet::new();
    let mut installer: Option<&str> = None;
    let mut current_key_has_installer = false;
    let mut current_key: Option<&str> = None;

    for line in &lines {
        if let Some(rest) = line.strip_prefix("#ROSDEP[") {
            // Flush previous key
            if let Some(key) = current_key {
                if !current_key_has_installer {
                    result.unresolved.push(key.to_string());
                }
            }
            let key = rest.trim_end_matches(']');
            seen_keys.insert(key.to_string());
            current_key = Some(key);
            current_key_has_installer = false;
            installer = None;
        } else if let Some(inst) = line.strip_prefix('#') {
            installer = Some(inst);
            if is_supported_installer(inst) {
                current_key_has_installer = true;
            }
        } else if let Some(inst) = installer {
            collect_packages(&mut result, inst, line);
        }
    }
    // Flush last key
    if let Some(key) = current_key {
        if !current_key_has_installer {
            result.unresolved.push(key.to_string());
        }
    }

    // Keys not even mentioned in the output
    for key in keys {
        if !seen_keys.contains(*key) {
            result.unresolved.push(key.to_string());
        }
    }

    Ok(result)
}

/// Returns `true` if the installer is supported (`apt` or `pip`).
fn is_supported_installer(installer: &str) -> bool {
    matches!(installer, "apt" | "pip")
}

/// Add space-separated package names to the appropriate installer bucket.
///
/// Only `apt` and `pip` are supported.  Other installers are silently
/// skipped here; callers use [`is_supported_installer`] to decide whether
/// to mark the key as resolved.
fn collect_packages(result: &mut ResolvedDeps, installer: &str, packages_line: &str) {
    let target = match installer {
        "apt" => &mut result.apt,
        "pip" => &mut result.pip,
        _other => return,
    };
    for pkg in packages_line.split_whitespace() {
        if !pkg.is_empty() {
            target.push(pkg.to_string());
        }
    }
}

/// Collect package names available in `AMENT_PREFIX_PATH` and `ROS_PACKAGE_PATH`.
///
/// Mirrors `rosdep install --ignore-src`: any key that corresponds to an
/// already-installed ament/catkin package is skipped.
fn installed_packages() -> HashSet<String> {
    let mut pkgs = HashSet::new();

    // AMENT_PREFIX_PATH: each entry is an install prefix like
    // /opt/ros/jazzy or <ws>/install/<pkg>.  Packages are at
    // <prefix>/share/<pkg_name>/package.xml.
    if let Ok(ament_path) = std::env::var("AMENT_PREFIX_PATH") {
        for prefix in ament_path.split(':').filter(|s| !s.is_empty()) {
            let share = std::path::Path::new(prefix).join("share");
            if let Ok(entries) = std::fs::read_dir(&share) {
                for entry in entries.flatten() {
                    if entry.path().join("package.xml").exists() {
                        if let Some(name) = entry.file_name().to_str() {
                            pkgs.insert(name.to_string());
                        }
                    }
                }
            }
        }
    }

    // ROS_PACKAGE_PATH: each entry is a directory containing packages
    // (catkin-style).  Walk one level and look for package.xml.
    if let Ok(ros_path) = std::env::var("ROS_PACKAGE_PATH") {
        for dir in ros_path.split(':').filter(|s| !s.is_empty()) {
            let path = std::path::Path::new(dir);
            if path.join("package.xml").exists() {
                if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                    pkgs.insert(name.to_string());
                }
            }
            if let Ok(entries) = std::fs::read_dir(path) {
                for entry in entries.flatten() {
                    if entry.path().join("package.xml").exists() {
                        if let Some(name) = entry.file_name().to_str() {
                            pkgs.insert(name.to_string());
                        }
                    }
                }
            }
        }
    }

    pkgs
}

/// Resolve rosdep keys and install the resulting system packages.
///
/// 1. Filter out keys already available as installed ROS packages
///    (checks `AMENT_PREFIX_PATH` and `ROS_PACKAGE_PATH`, like
///    `rosdep install --ignore-src`)
/// 2. `rosdep resolve` maps remaining keys → system package names
/// 3. `apt-get install` for `#apt` packages
/// 4. `pip install` for `#pip` packages
///
/// Returns an error if any keys could not be resolved.
pub fn rosdep_install(keys: &[&str]) -> crate::Result<()> {
    if keys.is_empty() {
        return Ok(());
    }

    // Filter out keys that are already installed as ROS packages.
    // Cached per-process: the orchestrator may call rosdep_install() many times.
    let installed = INSTALLED_PACKAGES.get_or_init(installed_packages);
    let mut filtered: Vec<&str> = keys
        .iter()
        .copied()
        .filter(|k| !installed.contains(*k))
        .collect();

    // Sort for deterministic ordering — callers often pass keys from a HashSet
    // whose iteration order varies between runs.
    filtered.sort_unstable();

    if filtered.len() < keys.len() {
        info!(
            "Skipped {} keys already installed as ROS packages ({} remaining)",
            keys.len() - filtered.len(),
            filtered.len()
        );
    }

    if filtered.is_empty() {
        return Ok(());
    }

    info!("Resolving {} rosdep keys", filtered.len());
    let resolved = rosdep_resolve(&filtered)?;

    if !resolved.unresolved.is_empty() {
        return Err(crate::Error::ProcessExecution(format!(
            "rosdep could not resolve {} keys: {:?}",
            resolved.unresolved.len(),
            resolved.unresolved
        )));
    }

    // Deduplicate — BTreeSet gives sorted, deterministic install order.
    let apt_pkgs: Vec<String> = resolved
        .apt
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let pip_pkgs: Vec<String> = resolved
        .pip
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();

    if !apt_pkgs.is_empty() {
        info!("Installing {} apt packages", apt_pkgs.len());
        let status = Command::new("sudo")
            .args(["apt-get", "install", "-y", "--no-install-recommends"])
            .args(&apt_pkgs)
            .status()
            .map_err(|e| {
                crate::Error::ProcessExecution(format!("failed to run apt-get install: {e}"))
            })?;

        if !status.success() {
            return Err(crate::Error::ProcessExecution(format!(
                "apt-get install failed (exit {})",
                status.code().unwrap_or(-1)
            )));
        }
    }

    if !pip_pkgs.is_empty() {
        info!("Installing {} pip packages", pip_pkgs.len());
        let status = Command::new("pip")
            .args(["install", "--break-system-packages"])
            .args(&pip_pkgs)
            .status()
            .map_err(|e| {
                crate::Error::ProcessExecution(format!("failed to run pip install: {e}"))
            })?;

        if !status.success() {
            return Err(crate::Error::ProcessExecution(format!(
                "pip install failed (exit {})",
                status.code().unwrap_or(-1)
            )));
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_single_key_apt() {
        let stdout = "#apt\nros-jazzy-rclcpp\n";
        let result = parse_rosdep_resolve(stdout, &["rclcpp"]).unwrap();
        assert_eq!(result.apt, vec!["ros-jazzy-rclcpp"]);
        assert!(result.pip.is_empty());
        assert!(result.unresolved.is_empty());
    }

    #[test]
    fn parse_multi_key() {
        let stdout = "\
#ROSDEP[rclcpp]
#apt
ros-jazzy-rclcpp
#ROSDEP[eigen]
#apt
libeigen3-dev
";
        let result = parse_rosdep_resolve(stdout, &["rclcpp", "eigen"]).unwrap();
        assert_eq!(result.apt, vec!["ros-jazzy-rclcpp", "libeigen3-dev"]);
        assert!(result.unresolved.is_empty());
    }

    #[test]
    fn parse_unresolved_key() {
        let stdout = "\
#ROSDEP[rclcpp]
#apt
ros-jazzy-rclcpp
#ROSDEP[nonexistent_xyz]
";
        let result = parse_rosdep_resolve(stdout, &["rclcpp", "nonexistent_xyz"]).unwrap();
        assert_eq!(result.apt, vec!["ros-jazzy-rclcpp"]);
        assert_eq!(result.unresolved, vec!["nonexistent_xyz"]);
    }

    #[test]
    fn parse_multi_packages_per_key() {
        let stdout = "\
#ROSDEP[libnl-3-dev]
#apt
libnl-3-dev libnl-genl-3-dev libnl-route-3-dev
";
        let result = parse_rosdep_resolve(stdout, &["libnl-3-dev"]).unwrap();
        assert_eq!(
            result.apt,
            vec!["libnl-3-dev", "libnl-genl-3-dev", "libnl-route-3-dev"]
        );
    }

    #[test]
    fn parse_empty_output() {
        let result = parse_rosdep_resolve("", &["foo", "bar"]).unwrap();
        assert_eq!(result.unresolved, vec!["foo", "bar"]);
    }

    #[test]
    fn parse_unsupported_installer_single_key() {
        let stdout = "#brew\nhomebrew-pkg\n";
        let result = parse_rosdep_resolve(stdout, &["some_key"]).unwrap();
        assert!(result.apt.is_empty());
        assert!(result.pip.is_empty());
        assert_eq!(result.unresolved, vec!["some_key"]);
    }

    #[test]
    fn parse_unsupported_installer_multi_key() {
        let stdout = "\
#ROSDEP[rclcpp]
#apt
ros-jazzy-rclcpp
#ROSDEP[brew_only]
#brew
homebrew-pkg
";
        let result = parse_rosdep_resolve(stdout, &["rclcpp", "brew_only"]).unwrap();
        assert_eq!(result.apt, vec!["ros-jazzy-rclcpp"]);
        assert_eq!(result.unresolved, vec!["brew_only"]);
    }
}
