//! Builder: compute a selective build plan and execute it for a resolved launch target.
//!
//! # Model
//!
//! The build plan is derived from the launch resolver output:
//!
//! 1. `ResolveResult::direct_packages` — packages directly referenced in the launch graph
//!    (node `pkg=` attributes and included packages).
//! 2. [`resolve_dependencies`] with [`DependencyMode::Build`] — expands the seed set
//!    transitively using `build_depend`, `<depend>`, `build_export_depend`, and
//!    `buildtool_depend` from each package's on-disk `package.xml`.
//!    `exec_depend` is intentionally excluded: launch-plus tracks runtime deps through the
//!    launch graph itself, making `exec_depend` redundant for this workflow.
//! 3. [`compute_build_order`] — Kahn's topological sort so dependencies are built first.
//! 4. Direct `cmake`/`make` (ament_cmake) or `setuptools` (ament_python) invocations per
//!    package, scheduled in parallel via a greedy worker pool.
//!
//! Transitive build dependencies that are not yet on disk are fetched iteratively:
//! each round expands the dep graph, fetches missing packages, and repeats until
//! no new packages appear.
//!
//! For `test` mode, [`DependencyMode::BuildAndTest`] adds `test_depend` packages
//! to the build set.

pub mod ament_cmake;
pub mod ament_python;
pub mod environment;
pub mod install;
pub mod scheduler;

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

/// Flag set by the SIGINT handler so the builder knows the child was interrupted.
static INTERRUPTED: AtomicBool = AtomicBool::new(false);

use crate::fetcher::{FetchOptions, fetch_packages};
use crate::indexer::{
    DependencyMode, Lockfile, compute_build_order, read_package_build_type, read_package_deps,
    resolve_dependencies,
};

/// Everything needed to build the launch target.
#[derive(Debug, Clone)]
pub struct BuildPlan {
    /// Lockfile packages to build, in topological order (dependencies first).
    pub packages: Vec<String>,
    /// External dependencies not in the lockfile (e.g. system ROS packages).
    ///
    /// These are discovered from `package.xml` `<depend>`, `<build_depend>`, etc.
    /// but are not part of the lockfile.  They must be installed via `rosdep` or
    /// `apt` before building.
    pub external_deps: HashSet<String>,
    /// Source directory containing fetched repositories.
    pub src_dir: PathBuf,
    /// Build output directory (per-package build artifacts).
    pub build_base: PathBuf,
    /// Install prefix (per-package install trees).
    pub install_base: PathBuf,
    /// Log directory.
    pub log_base: PathBuf,
}

/// Options controlling how the build is executed.
#[derive(Debug, Clone)]
pub struct BuildOptions {
    /// Print the build commands without running them.
    pub dry_run: bool,
    /// Maximum number of parallel build workers.
    pub parallel_workers: usize,
    /// Extra arguments passed to cmake (e.g. `-DCMAKE_BUILD_TYPE=Release`).
    pub cmake_args: Vec<String>,
    /// Extra arguments passed to make.
    pub make_args: Vec<String>,
    /// Use symlink install instead of copying files.
    pub symlink_install: bool,
    /// Continue building independent packages after a failure.
    pub continue_on_error: bool,
    /// Test mode: sets `BUILD_TESTING=ON` and uses `BuildAndTest` dep mode.
    pub test_mode: bool,
}

impl Default for BuildOptions {
    fn default() -> Self {
        Self {
            dry_run: false,
            parallel_workers: std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(4),
            cmake_args: Vec::new(),
            make_args: Vec::new(),
            symlink_install: false,
            continue_on_error: false,
            test_mode: false,
        }
    }
}

/// Compute a build plan from explicit seed packages, fetching transitive deps as needed.
///
/// Expands transitively by reading on-disk `package.xml`.  Packages whose
/// `package.xml` is not yet on disk are fetched, and the expansion is retried
/// until the graph stabilises (up to 10 rounds).
pub fn plan_build_from_packages(
    seed_packages: &std::collections::HashSet<String>,
    lockfile: &Lockfile,
    src_dir: &Path,
    build_base: &Path,
    install_base: &Path,
    log_base: &Path,
    fetch_options: &FetchOptions,
    test_mode: bool,
) -> crate::Result<BuildPlan> {
    let mode = if test_mode {
        DependencyMode::BuildAndTest
    } else {
        DependencyMode::Build
    };

    // Iterative expand+fetch: each round may discover new packages whose
    // package.xml wasn't on disk yet.
    let mut graph = resolve_dependencies(lockfile, src_dir, seed_packages, mode);
    for _ in 0..10 {
        if graph.missing.is_empty() {
            break;
        }

        let missing: Vec<String> = graph.missing.iter().cloned().collect();
        tracing::info!(
            "Fetching {} transitive build deps not yet on disk: {:?}",
            missing.len(),
            missing
        );
        let fetched = fetch_packages(&missing, lockfile, src_dir, fetch_options)?;
        if fetched.is_empty() {
            break; // nothing could be fetched — stop
        }

        // Re-expand now that more package.xml files are available.
        graph = resolve_dependencies(lockfile, src_dir, seed_packages, mode);
    }

    let packages = compute_build_order(lockfile, src_dir, &graph.packages);

    Ok(BuildPlan {
        packages,
        external_deps: graph.external,
        src_dir: src_dir.to_path_buf(),
        build_base: build_base.to_path_buf(),
        install_base: install_base.to_path_buf(),
        log_base: log_base.to_path_buf(),
    })
}

/// Execute the build plan using the native builder backend.
///
/// Steps:
/// 1. Determine build type for each package (ament_cmake or ament_python)
/// 2. Validate: reject unsupported build types
/// 3. Create timestamped log directory with `latest` symlink
/// 4. Create root install layout (setup.bash, setup.sh, setup.zsh, etc.)
/// 5. Run the parallel scheduler
/// 6. Create per-package install metadata for successfully built packages
///
/// When `options.dry_run` is true, prints the build commands without executing.
#[allow(unsafe_code)]
pub fn execute_build(
    plan: &BuildPlan,
    options: &BuildOptions,
    lockfile: &Lockfile,
) -> crate::Result<()> {
    if plan.packages.is_empty() {
        tracing::info!("No packages to build.");
        return Ok(());
    }

    // 1. Determine build type for each package.
    let mut build_types: HashMap<String, scheduler::BuildType> = HashMap::new();
    for pkg in &plan.packages {
        let bt = match read_package_build_type(lockfile, &plan.src_dir, pkg) {
            Some(ref s) if s == "ament_python" => scheduler::BuildType::AmentPython,
            Some(ref s) if s == "ament_cmake" => scheduler::BuildType::AmentCmake,
            None => scheduler::BuildType::AmentCmake, // default
            Some(other) => {
                return Err(crate::Error::UnsupportedBuildType {
                    package: pkg.clone(),
                    build_type: other,
                });
            }
        };
        build_types.insert(pkg.clone(), bt);
    }

    // 2. Build per-package dependency sets (in-set build deps only).
    let mut pkg_deps: HashMap<String, HashSet<String>> = HashMap::new();
    let pkg_set: HashSet<&String> = plan.packages.iter().collect();
    for pkg in &plan.packages {
        let mut deps = HashSet::new();
        if let Some(d) = read_package_deps(lockfile, &plan.src_dir, pkg) {
            for dep_name in d
                .build
                .iter()
                .chain(d.build_export.iter())
                .chain(d.buildtool.iter())
                .chain(d.buildtool_export.iter())
            {
                if pkg_set.contains(dep_name) {
                    deps.insert(dep_name.clone());
                }
            }
        }
        pkg_deps.insert(pkg.clone(), deps);
    }

    // 3. Create timestamped log directory with `latest` symlink.
    let timestamp = chrono_timestamp();
    let log_dir = plan.log_base.join(&timestamp);
    std::fs::create_dir_all(&log_dir)?;
    let latest_link = plan.log_base.join("latest");
    // Remove existing symlink and create new one.
    #[cfg(unix)]
    {
        let _ = std::fs::remove_file(&latest_link);
        let _ = std::os::unix::fs::symlink(&timestamp, &latest_link);
    }

    // Use the timestamped log dir for this build.
    let mut plan = plan.clone();
    plan.log_base = log_dir;

    // 4. Create root install layout.
    let parent_prefix = std::env::var("COLCON_PREFIX_PATH")
        .or_else(|_| std::env::var("AMENT_PREFIX_PATH"))
        .ok();
    install::create_root_layout(&plan.install_base, parent_prefix.as_deref())?;

    // 5. Install SIGINT handler and run scheduler.
    INTERRUPTED.store(false, Ordering::SeqCst);

    #[cfg(unix)]
    let prev_handler = unsafe {
        libc::signal(
            libc::SIGINT,
            sigint_handler as *const () as libc::sighandler_t,
        )
    };

    let result =
        scheduler::run_parallel_build(&plan, options, &build_types, &pkg_deps, &INTERRUPTED)?;

    #[cfg(unix)]
    unsafe {
        libc::signal(libc::SIGINT, prev_handler);
    }

    // 6. Create per-package install metadata for successfully built packages.
    for pkg in &result.completed {
        // Read runtime deps (exec_depend) for the colcon metadata files.
        let runtime_deps: Vec<String> =
            if let Some(d) = read_package_deps(lockfile, &plan.src_dir, pkg) {
                d.exec
            } else {
                vec![]
            };
        let has_library = plan.install_base.join(pkg).join("lib").join(pkg).exists()
            || plan.install_base.join(pkg).join("lib").is_dir();
        install::create_package_install_metadata(
            &plan.install_base.join(pkg),
            pkg,
            &runtime_deps,
            has_library,
        )?;
    }

    // 7. Report results.
    if result.is_success() {
        eprintln!(
            "\nBuild complete: {} packages built successfully.",
            result.completed.len()
        );
        Ok(())
    } else {
        let failed_names: Vec<&str> = result.failed.iter().map(|(n, _)| n.as_str()).collect();
        Err(crate::Error::BuildFailed {
            package: failed_names.join(", "),
            detail: format!(
                "{} succeeded, {} failed",
                result.completed.len(),
                result.failed.len()
            ),
        })
    }
}

/// Generate an ISO-8601 timestamp string for log directory naming.
fn chrono_timestamp() -> String {
    use std::time::SystemTime;
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs();
    // Simple formatting without chrono dependency.
    // Format: YYYY-MM-DD_HH-MM-SS
    let days = secs / 86400;
    let time_of_day = secs % 86400;
    let hours = time_of_day / 3600;
    let minutes = (time_of_day % 3600) / 60;
    let seconds = time_of_day % 60;

    // Approximate date calculation (good enough for log dirs).
    let mut year = 1970i64;
    #[allow(clippy::cast_possible_wrap)]
    let mut remaining_days = days as i64;
    loop {
        let days_in_year = if is_leap_year(year) { 366 } else { 365 };
        if remaining_days < days_in_year {
            break;
        }
        remaining_days -= days_in_year;
        year += 1;
    }
    let month_days = if is_leap_year(year) {
        [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    } else {
        [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    };
    let mut month = 1;
    for &md in &month_days {
        if remaining_days < md {
            break;
        }
        remaining_days -= md;
        month += 1;
    }
    let day = remaining_days + 1;

    format!("{year:04}-{month:02}-{day:02}_{hours:02}-{minutes:02}-{seconds:02}")
}

fn is_leap_year(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)
}

/// Signal-safe SIGINT handler: sets the INTERRUPTED flag.
///
/// Child processes in the same process group receive SIGINT directly from
/// the terminal.  This handler prevents our process from being killed
/// before we can clean up.
#[cfg(unix)]
#[allow(unsafe_code)]
extern "C" fn sigint_handler(_sig: libc::c_int) {
    INTERRUPTED.store(true, Ordering::SeqCst);
}
