//! Greedy parallel build scheduler.
//!
//! Maintains a dependency graph and dispatches ready packages to a worker
//! pool of N threads.  Each worker picks the next ready package, builds it
//! via the appropriate backend (ament_cmake or ament_python), and marks it
//! done — which may unlock further packages.
//!
//! Supports `--continue-on-error` (keep building independent packages after
//! a failure) and SIGINT (stop accepting new work, wait for in-flight builds).

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::Instant;

use super::ament_cmake::{BuildOptionsRef, PackageBuildContext};
use super::environment::build_env_for_package;
use super::{BuildOptions, BuildPlan};

/// Build type for a package, determined from `<export><build_type>` in package.xml.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BuildType {
    AmentCmake,
    AmentPython,
}

/// Per-package node in the scheduler's dependency graph.
#[derive(Debug)]
struct PackageNode {
    build_type: BuildType,
    /// Number of in-set dependencies that haven't finished yet.
    remaining_deps: usize,
    /// Packages that depend on this one (will have their remaining_deps decremented).
    dependents: Vec<String>,
}

/// Mutable scheduler state protected by a mutex.
struct SchedulerState {
    /// Packages ready to build (all deps satisfied).
    ready: VecDeque<String>,
    /// Packages currently being built.
    in_progress: HashSet<String>,
    /// Successfully completed packages, in completion order.
    completed: Vec<String>,
    /// Failed packages: (name, error message).
    failed: Vec<(String, String)>,
    /// Set on first failure when `!continue_on_error` — workers stop picking up new work.
    poisoned: bool,
    /// Per-package nodes.
    nodes: HashMap<String, PackageNode>,
    /// Total package count (for progress display).
    total: usize,
    /// Counter for progress display.
    finished_count: usize,
}

/// Result of a parallel build run.
#[derive(Debug)]
pub struct BuildResult {
    /// Successfully built packages, in completion order.
    pub completed: Vec<String>,
    /// Failed packages: (name, error message).
    pub failed: Vec<(String, String)>,
}

impl BuildResult {
    pub fn is_success(&self) -> bool {
        self.failed.is_empty()
    }
}

/// Run a parallel build of all packages in the plan.
///
/// # Arguments
/// * `plan` - Build plan with packages in topological order
/// * `options` - Build options
/// * `build_types` - Map of package name → build type
/// * `pkg_deps` - Map of package name → set of in-set build dependencies
/// * `interrupted` - Global SIGINT flag
pub fn run_parallel_build(
    plan: &BuildPlan,
    options: &BuildOptions,
    build_types: &HashMap<String, BuildType>,
    pkg_deps: &HashMap<String, HashSet<String>>,
    interrupted: &'static AtomicBool,
) -> crate::Result<BuildResult> {
    if plan.packages.is_empty() {
        return Ok(BuildResult {
            completed: vec![],
            failed: vec![],
        });
    }

    let pkg_set: HashSet<&String> = plan.packages.iter().collect();

    // Build the dependency graph for the scheduler.
    let mut nodes: HashMap<String, PackageNode> = HashMap::new();

    for pkg in &plan.packages {
        let build_type = build_types
            .get(pkg)
            .copied()
            .unwrap_or(BuildType::AmentCmake);
        let deps = pkg_deps.get(pkg).cloned().unwrap_or_default();
        // Only count in-set deps.
        let in_set_deps: HashSet<String> =
            deps.into_iter().filter(|d| pkg_set.contains(d)).collect();
        nodes.insert(
            pkg.clone(),
            PackageNode {
                build_type,
                remaining_deps: in_set_deps.len(),
                dependents: Vec::new(),
            },
        );
    }

    // Fill in dependents (reverse edges).
    for pkg in &plan.packages {
        let deps = pkg_deps.get(pkg).cloned().unwrap_or_default();
        for dep in deps {
            if let Some(node) = nodes.get_mut(&dep) {
                node.dependents.push(pkg.clone());
            }
        }
    }

    // Find initially ready packages (no in-set deps).
    let mut ready: VecDeque<String> = VecDeque::new();
    for pkg in &plan.packages {
        if nodes[pkg].remaining_deps == 0 {
            ready.push_back(pkg.clone());
        }
    }

    let total = plan.packages.len();
    let state = Mutex::new(SchedulerState {
        ready,
        in_progress: HashSet::new(),
        completed: Vec::new(),
        failed: Vec::new(),
        poisoned: false,
        nodes,
        total,
        finished_count: 0,
    });
    let cvar = Condvar::new();
    let opts_ref = BuildOptionsRef::from(options);

    let worker_count = options.parallel_workers.min(total);

    std::thread::scope(|s| {
        for _worker_id in 0..worker_count {
            let state = &state;
            let cvar = &cvar;
            let opts_ref = &opts_ref;
            let plan = &plan;

            s.spawn(move || {
                worker_loop(
                    state,
                    cvar,
                    opts_ref,
                    plan,
                    interrupted,
                    options.continue_on_error,
                );
            });
        }
    });

    let guard = state.lock().unwrap();
    Ok(BuildResult {
        completed: guard.completed.clone(),
        failed: guard.failed.clone(),
    })
}

/// Main worker loop: pick ready → build → report → repeat.
fn worker_loop(
    state: &Mutex<SchedulerState>,
    cvar: &Condvar,
    opts_ref: &BuildOptionsRef,
    plan: &BuildPlan,
    interrupted: &'static AtomicBool,
    continue_on_error: bool,
) {
    loop {
        // === Pick next package ===
        let (pkg_name, build_type, built_deps) = {
            let mut guard = state.lock().unwrap();

            // Wait until there's work or we should exit.
            loop {
                if interrupted.load(Ordering::SeqCst) || guard.poisoned {
                    return;
                }
                if let Some(pkg) = guard.ready.pop_front() {
                    let bt = guard.nodes[&pkg].build_type;
                    let deps = guard.completed.clone();
                    guard.in_progress.insert(pkg.clone());
                    break (pkg, bt, deps);
                }
                // No work available — check if we're done.
                if guard.in_progress.is_empty() {
                    // Nothing in progress, nothing ready → all done or blocked.
                    return;
                }
                guard = cvar.wait(guard).unwrap();
            }
        };

        // === Build the package ===
        let start = Instant::now();
        let build_env = build_env_for_package(&plan.install_base, &built_deps);

        let ctx = PackageBuildContext {
            pkg_name: pkg_name.clone(),
            src_dir: find_package_dir(&plan.src_dir, &pkg_name),
            build_dir: plan.build_base.join(&pkg_name),
            install_dir: plan.install_base.join(&pkg_name),
            log_dir: plan.log_base.join(&pkg_name),
            env: build_env,
            options: opts_ref.clone(),
            interrupted,
        };

        // Progress: starting.
        {
            let guard = state.lock().unwrap();
            eprintln!(
                "[{}/{}] Building {} ({})",
                guard.finished_count + guard.in_progress.len(),
                guard.total,
                pkg_name,
                match build_type {
                    BuildType::AmentCmake => "ament_cmake",
                    BuildType::AmentPython => "ament_python",
                }
            );
        }

        let result = match build_type {
            BuildType::AmentCmake => super::ament_cmake::build_ament_cmake(&ctx),
            BuildType::AmentPython => super::ament_python::build_ament_python(&ctx),
        };

        let elapsed = start.elapsed();

        // === Report result ===
        {
            let mut guard = state.lock().unwrap();
            guard.in_progress.remove(&pkg_name);
            guard.finished_count += 1;

            match result {
                Ok(()) => {
                    eprintln!(
                        "[{}/{}] \u{2713} {} ({:.1}s)",
                        guard.finished_count,
                        guard.total,
                        pkg_name,
                        elapsed.as_secs_f64()
                    );

                    guard.completed.push(pkg_name.clone());

                    // Unlock dependents.
                    let dependents = guard.nodes[&pkg_name].dependents.clone();
                    for dep_pkg in dependents {
                        if let Some(node) = guard.nodes.get_mut(&dep_pkg) {
                            node.remaining_deps -= 1;
                            if node.remaining_deps == 0 && !guard.poisoned {
                                guard.ready.push_back(dep_pkg);
                            }
                        }
                    }
                }
                Err(e) => {
                    eprintln!(
                        "[{}/{}] \u{2717} {} ({:.1}s): {}",
                        guard.finished_count,
                        guard.total,
                        pkg_name,
                        elapsed.as_secs_f64(),
                        e
                    );

                    guard.failed.push((pkg_name.clone(), e.to_string()));

                    if !continue_on_error {
                        guard.poisoned = true;
                    }
                }
            }
        }

        cvar.notify_all();
    }
}

/// Search for a package's source directory by looking for package.xml files.
///
/// Checks direct subdirectory first, then walks up to 3 levels deep.
/// Falls back to `src_dir/<pkg_name>` if not found.
fn find_package_dir(src_dir: &Path, pkg_name: &str) -> std::path::PathBuf {
    // Fast path: direct subdirectory.
    let direct = src_dir.join(pkg_name);
    if direct.join("package.xml").exists() {
        return direct;
    }

    // Walk up to 4 levels deep looking for the package.
    for entry in walkdir_limited(src_dir, 4) {
        if entry.file_name() == Some(std::ffi::OsStr::new("package.xml")) {
            if let Some(parent) = entry.parent() {
                if parent.file_name().map(|n| n.to_string_lossy()) == Some(pkg_name.into()) {
                    return parent.to_path_buf();
                }
            }
        }
    }

    // Fallback.
    direct
}

/// Simple directory walker limited to `max_depth` levels.
fn walkdir_limited(root: &Path, max_depth: usize) -> Vec<std::path::PathBuf> {
    let mut result = Vec::new();
    let mut stack: Vec<(std::path::PathBuf, usize)> = vec![(root.to_path_buf(), 0)];

    while let Some((dir, depth)) = stack.pop() {
        if depth >= max_depth {
            continue;
        }
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push((path, depth + 1));
            } else {
                result.push(path);
            }
        }
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    #[test]
    fn test_empty_build() {
        static INTERRUPTED: AtomicBool = AtomicBool::new(false);

        let tmp = TempDir::new().unwrap();
        let plan = BuildPlan {
            packages: vec![],
            external_deps: HashSet::new(),
            src_dir: tmp.path().join("src"),
            build_base: tmp.path().join("build"),
            install_base: tmp.path().join("install"),
            log_base: tmp.path().join("log"),
        };
        let options = BuildOptions::default();

        let result = run_parallel_build(
            &plan,
            &options,
            &HashMap::new(),
            &HashMap::new(),
            &INTERRUPTED,
        )
        .unwrap();

        assert!(result.is_success());
        assert!(result.completed.is_empty());
    }

    #[test]
    fn test_find_package_dir_direct() {
        let tmp = TempDir::new().unwrap();
        let pkg_dir = tmp.path().join("my_pkg");
        fs::create_dir_all(&pkg_dir).unwrap();
        fs::write(pkg_dir.join("package.xml"), "<package/>").unwrap();

        let found = find_package_dir(tmp.path(), "my_pkg");
        assert_eq!(found, pkg_dir);
    }

    #[test]
    fn test_find_package_dir_nested() {
        let tmp = TempDir::new().unwrap();
        let pkg_dir = tmp.path().join("some_repo").join("my_pkg");
        fs::create_dir_all(&pkg_dir).unwrap();
        fs::write(pkg_dir.join("package.xml"), "<package/>").unwrap();

        let found = find_package_dir(tmp.path(), "my_pkg");
        assert_eq!(found, pkg_dir);
    }

    #[test]
    fn test_find_package_dir_fallback() {
        let tmp = TempDir::new().unwrap();
        let found = find_package_dir(tmp.path(), "nonexistent");
        assert_eq!(found, tmp.path().join("nonexistent"));
    }
}
