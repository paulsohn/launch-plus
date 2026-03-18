//! Build environment computation: CMAKE_PREFIX_PATH, AMENT_PREFIX_PATH, etc.
//!
//! Computes the environment variables needed to build each package given
//! its already-built dependencies.  The scheduler calls [`build_env_for_package`]
//! before launching each package build.

use std::collections::HashMap;
use std::path::Path;

/// Compute the environment variables needed to build a package.
///
/// The returned map contains variables that should be **set** (not appended to)
/// in the child process environment.  The caller should merge these with the
/// inherited process environment, with these values taking precedence.
///
/// # Arguments
/// * `install_base` - The install root (e.g. `install/`)
/// * `built_deps` - Names of packages already built and installed under
///   `install_base/<dep>/`.  Must include only in-set dependencies that have
///   been successfully built, in dependency order.
pub fn build_env_for_package(
    install_base: &Path,
    built_deps: &[String],
) -> HashMap<String, String> {
    let mut env = HashMap::new();

    if built_deps.is_empty() {
        return env;
    }

    // Build the prefix paths from built dependencies.
    // Most-recently-built (deepest dep) goes first so cmake finds the
    // closest override, matching colcon behavior.
    let prefixes: Vec<String> = built_deps
        .iter()
        .rev()
        .map(|dep| install_base.join(dep).to_string_lossy().into_owned())
        .collect();

    // CMAKE_PREFIX_PATH: semicolons on Windows, colons on Unix.
    // Append existing system CMAKE_PREFIX_PATH.
    let cmake_prefix = build_path_var(&prefixes, "CMAKE_PREFIX_PATH");
    env.insert("CMAKE_PREFIX_PATH".to_string(), cmake_prefix);

    // AMENT_PREFIX_PATH
    let ament_prefix = build_path_var(&prefixes, "AMENT_PREFIX_PATH");
    env.insert("AMENT_PREFIX_PATH".to_string(), ament_prefix);

    // LD_LIBRARY_PATH: <prefix>/lib for each dep
    let lib_paths: Vec<String> = built_deps
        .iter()
        .rev()
        .map(|dep| {
            install_base
                .join(dep)
                .join("lib")
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    let ld_path = build_path_var(&lib_paths, "LD_LIBRARY_PATH");
    env.insert("LD_LIBRARY_PATH".to_string(), ld_path);

    // PATH: <prefix>/bin for each dep
    let bin_paths: Vec<String> = built_deps
        .iter()
        .rev()
        .map(|dep| {
            install_base
                .join(dep)
                .join("bin")
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    let path = build_path_var(&bin_paths, "PATH");
    env.insert("PATH".to_string(), path);

    // PYTHONPATH: <prefix>/lib/python3.*/site-packages for each dep.
    // We detect the python version from the system.
    let python_subdir = detect_python_site_packages_subdir();
    let python_paths: Vec<String> = built_deps
        .iter()
        .rev()
        .map(|dep| {
            install_base
                .join(dep)
                .join(&python_subdir)
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    let pythonpath = build_path_var(&python_paths, "PYTHONPATH");
    env.insert("PYTHONPATH".to_string(), pythonpath);

    env
}

/// Build a colon-separated path variable by prepending `new_paths` to the
/// current value of `var_name` from the process environment.
fn build_path_var(new_paths: &[String], var_name: &str) -> String {
    let parts: Vec<&str> = new_paths.iter().map(|s| s.as_str()).collect();

    // Append existing value from the process environment.
    if let Ok(existing) = std::env::var(var_name) {
        if !existing.is_empty() {
            // We need to own the string to extend lifetime.
            // Use a slightly different approach: build the full string.
            let joined = parts.join(":");
            return format!("{joined}:{existing}");
        }
    }

    parts.join(":")
}

/// Detect the Python site-packages subdirectory (e.g. `lib/python3.10/site-packages`).
///
/// Falls back to `lib/python3/dist-packages` if detection fails.
fn detect_python_site_packages_subdir() -> String {
    // Try to detect from the running system.
    if let Ok(output) = std::process::Command::new("python3")
        .args(["-c", "import sys; print(f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')"])
        .output()
    {
        if output.status.success() {
            let s = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !s.is_empty() {
                return s;
            }
        }
    }
    // Fallback
    "lib/python3/dist-packages".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_build_env_empty_deps() {
        let tmp = TempDir::new().unwrap();
        let env = build_env_for_package(tmp.path(), &[]);
        assert!(env.is_empty());
    }

    #[test]
    fn test_build_env_single_dep() {
        let tmp = TempDir::new().unwrap();
        let install = tmp.path().join("install");
        std::fs::create_dir_all(&install).unwrap();

        let deps = vec!["rclcpp".to_string()];
        let env = build_env_for_package(&install, &deps);

        let cmake = env.get("CMAKE_PREFIX_PATH").unwrap();
        assert!(cmake.contains("rclcpp"));

        let ament = env.get("AMENT_PREFIX_PATH").unwrap();
        assert!(ament.contains("rclcpp"));

        let ld = env.get("LD_LIBRARY_PATH").unwrap();
        assert!(ld.contains("rclcpp/lib"));

        let path = env.get("PATH").unwrap();
        assert!(path.contains("rclcpp/bin"));

        let pypath = env.get("PYTHONPATH").unwrap();
        assert!(pypath.contains("rclcpp"));
    }

    #[test]
    fn test_build_env_multiple_deps_order() {
        let tmp = TempDir::new().unwrap();
        let install = tmp.path().join("install");
        std::fs::create_dir_all(&install).unwrap();

        // deps are in topo order (dep first, then dependent)
        let deps = vec!["base".to_string(), "mid".to_string(), "top".to_string()];
        let env = build_env_for_package(&install, &deps);

        let cmake = env.get("CMAKE_PREFIX_PATH").unwrap();
        // Most recent (deepest/last in list) should come first in the path
        let top_pos = cmake.find("top").unwrap();
        let base_pos = cmake.find("base").unwrap();
        assert!(
            top_pos < base_pos,
            "top should appear before base in CMAKE_PREFIX_PATH"
        );
    }

    #[test]
    fn test_detect_python_site_packages() {
        let subdir = detect_python_site_packages_subdir();
        // Should match lib/python3.X/site-packages or fallback
        assert!(
            subdir.starts_with("lib/python3"),
            "unexpected python subdir: {subdir}"
        );
    }
}
