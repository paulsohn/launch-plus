//! Builder: compute a selective colcon build plan from a resolved launch target.
//!
//! # Model
//!
//! The build plan is derived from the launch resolver output:
//!
//! 1. [`ResolveResult::direct_packages`] — packages directly referenced in the launch graph
//!    (node `pkg=` attributes and included packages).
//! 2. [`resolve_dependencies`] with [`DependencyMode::Build`] — expands the seed set
//!    transitively using `build_depend`, `<depend>`, `build_export_depend`, and
//!    `buildtool_depend` from each package's on-disk `package.xml`.
//!    `exec_depend` is intentionally excluded: launch-plus tracks runtime deps through the
//!    launch graph itself, making `exec_depend` redundant for this workflow.
//! 3. [`compute_build_order`] — Kahn's topological sort so dependencies are built first.
//! 4. `colcon build --packages-select <ordered list>` — exact list, not `--packages-up-to`,
//!    so colcon's native dep resolver is bypassed entirely.
//!
//! Transitive build dependencies that are not yet on disk are fetched iteratively:
//! each round expands the dep graph, fetches missing packages, and repeats until
//! no new packages appear.
//!
//! For `test` mode, [`DependencyMode::All`] adds `test_depend` packages to the build set.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};

/// Flag set by the SIGINT handler so the builder knows the child was interrupted.
static INTERRUPTED: AtomicBool = AtomicBool::new(false);

use crate::fetcher::{FetchOptions, fetch_packages};
use crate::indexer::{DependencyMode, Lockfile, compute_build_order, resolve_dependencies};

/// Everything colcon needs to build the launch target.
#[derive(Debug, Clone)]
pub struct BuildPlan {
    /// Lockfile packages to build, in topological order (dependencies first).
    pub packages: Vec<String>,
    /// Source directory passed to `colcon build --base-paths`.
    pub src_dir: PathBuf,
    /// Colcon build output directory (`--build-base`).
    pub build_base: PathBuf,
    /// Colcon install prefix (`--install-base`).
    pub install_base: PathBuf,
}

/// Options controlling how the build is executed.
#[derive(Debug, Clone, Default)]
pub struct BuildOptions {
    /// Print the colcon command without running it.
    pub dry_run: bool,
    /// Extra arguments inserted into `colcon build` before `--packages-select`.
    ///
    /// Typically loaded from a flagfile (one token per line).  Any colcon flag is
    /// accepted: `--symlink-install`, `--parallel-workers 8`, `--cmake-args
    /// -DCMAKE_BUILD_TYPE=Release`, `--allow-overriding pkg`, etc.
    pub extra_colcon_args: Vec<String>,
}

/// Compute a build plan from explicit seed packages, fetching transitive deps as needed.
///
/// Expands transitively by reading on-disk `package.xml`.  Packages whose
/// `package.xml` is not yet on disk are fetched, and the expansion is retried
/// until the graph stabilises (up to 10 rounds).
///
/// # Why `BuildAndExec` instead of `Build`
///
/// Ideally only `Build` deps would be needed here, since `exec_depend` packages
/// are not required for compilation.  However, colcon's ament_cmake task validates
/// that **all** `package.xml` dependencies (including `exec_depend`) have install
/// artifacts before running cmake.  There is no flag to disable this check.
///
/// TODO(milestone): replace colcon with direct cmake invocations so that only
/// true build dependencies need to be compiled.
pub fn plan_build_from_packages(
    seed_packages: &std::collections::HashSet<String>,
    lockfile: &Lockfile,
    src_dir: &Path,
    build_base: &Path,
    install_base: &Path,
    fetch_options: &FetchOptions,
    test_mode: bool,
) -> crate::Result<BuildPlan> {
    // TODO: switch to DependencyMode::Build once we replace colcon with direct
    // cmake invocations (colcon forces exec_depend validation at install time).
    let mode = if test_mode {
        DependencyMode::All
    } else {
        DependencyMode::BuildAndExec
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
        src_dir: src_dir.to_path_buf(),
        build_base: build_base.to_path_buf(),
        install_base: install_base.to_path_buf(),
    })
}

/// Execute `colcon build --packages-select <packages>`.
///
/// When `options.dry_run` is true, prints the command to stdout without running it.
#[allow(unsafe_code)]
pub fn execute_build(plan: &BuildPlan, options: &BuildOptions) -> crate::Result<()> {
    if plan.packages.is_empty() {
        tracing::info!("No packages to build.");
        return Ok(());
    }

    let mut args: Vec<String> = vec!["build".to_string()];

    args.push("--base-paths".to_string());
    args.push(plan.src_dir.to_string_lossy().into_owned());

    args.push("--build-base".to_string());
    args.push(plan.build_base.to_string_lossy().into_owned());

    args.push("--install-base".to_string());
    args.push(plan.install_base.to_string_lossy().into_owned());

    args.extend(options.extra_colcon_args.iter().cloned());

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
    let prev_handler = unsafe { libc::signal(libc::SIGINT, sigint_handler as libc::sighandler_t) };

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
        return Err(crate::Error::ProcessExecution(
            "build interrupted by Ctrl+C".to_string(),
        ));
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
