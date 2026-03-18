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

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};

/// Flag set by the SIGINT handler so the builder knows the child was interrupted.
static INTERRUPTED: AtomicBool = AtomicBool::new(false);

use crate::fetcher::{FetchOptions, fetch_packages};
use crate::indexer::{DependencyMode, Lockfile, compute_build_order, resolve_dependencies};

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

/// Execute the build plan using colcon as a subprocess.
///
/// This is the legacy colcon-based executor, retained as a fallback during
/// the transition to the native builder backend. Once the native backend
/// (ament_cmake, ament_python, scheduler) is complete, this function will
/// be replaced.
///
/// When `options.dry_run` is true, prints the command to stdout without running it.
#[allow(unsafe_code)]
pub fn execute_build(plan: &BuildPlan, options: &BuildOptions) -> crate::Result<()> {
    if plan.packages.is_empty() {
        tracing::info!("No packages to build.");
        return Ok(());
    }

    // --log-base is a colcon global argument (before the verb).
    let mut args: Vec<String> = vec![
        "--log-base".to_string(),
        plan.log_base.to_string_lossy().into_owned(),
        "build".to_string(),
    ];

    args.push("--base-paths".to_string());
    args.push(plan.src_dir.to_string_lossy().into_owned());

    args.push("--build-base".to_string());
    args.push(plan.build_base.to_string_lossy().into_owned());

    args.push("--install-base".to_string());
    args.push(plan.install_base.to_string_lossy().into_owned());

    // Map new BuildOptions fields to colcon args for the legacy path.
    if options.symlink_install {
        args.push("--symlink-install".to_string());
    }
    if options.continue_on_error {
        args.push("--continue-on-error".to_string());
    }
    if !options.cmake_args.is_empty() {
        args.push("--cmake-args".to_string());
        args.extend(options.cmake_args.iter().cloned());
    }

    args.push("--packages-select".to_string());
    args.extend(plan.packages.iter().cloned());

    let cmd_str = format!("colcon {}", args.join(" "));

    if options.dry_run {
        println!("{cmd_str}");
        return Ok(());
    }

    tracing::info!("{cmd_str}");

    // Spawn colcon as a child process and forward SIGINT so a single Ctrl+C
    // terminates both colcon and the builder.
    let mut child = Command::new("colcon")
        .args(&args)
        .spawn()
        .map_err(|e| crate::Error::ProcessExecution(format!("failed to run colcon: {e}")))?;

    INTERRUPTED.store(false, Ordering::SeqCst);

    // Install a SIGINT handler that sets the flag instead of killing our process.
    // The default SIGINT behaviour is restored after the child exits.
    #[cfg(unix)]
    let prev_handler = unsafe {
        libc::signal(
            libc::SIGINT,
            sigint_handler as *const () as libc::sighandler_t,
        )
    };

    let status = child
        .wait()
        .map_err(|e| crate::Error::ProcessExecution(format!("failed to wait for colcon: {e}")))?;

    // Restore previous signal handler.
    #[cfg(unix)]
    unsafe {
        libc::signal(libc::SIGINT, prev_handler);
    }

    if INTERRUPTED.load(Ordering::SeqCst) {
        // Child was killed by our forwarded signal; propagate as an error.
        return Err(crate::Error::BuildInterrupted);
    }

    if !status.success() {
        return Err(crate::Error::ProcessExecution(format!(
            "colcon build failed with status: {status}"
        )));
    }
    Ok(())
}

/// Signal-safe SIGINT handler: sets the INTERRUPTED flag.
///
/// The child process (colcon) is in the same process group and receives SIGINT
/// directly from the terminal.  This handler prevents our process from being
/// killed before we can clean up.
#[cfg(unix)]
extern "C" fn sigint_handler(_sig: libc::c_int) {
    use std::sync::atomic::Ordering;
    // AtomicBool::store is signal-safe.
    INTERRUPTED.store(true, Ordering::SeqCst);
}
