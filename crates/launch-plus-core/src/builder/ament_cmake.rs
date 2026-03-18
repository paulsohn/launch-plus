//! ament_cmake build backend: cmake configure / make / make install for a single package.
//!
//! Implements the three-step build process that ament_cmake packages use:
//! 1. **Configure**: `cmake` with install prefix, BUILD_TESTING, and user args
//! 2. **Build**: `make -jN` with user args
//! 3. **Install**: `make install`
//!
//! The caller (scheduler) is responsible for setting up the build environment
//! (CMAKE_PREFIX_PATH, etc.) via [`super::environment::build_env_for_package`].

use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

/// Everything a per-package build function needs.
///
/// Shared between `ament_cmake` and `ament_python` backends.
#[derive(Debug)]
pub struct PackageBuildContext {
    /// Package name (e.g. `autoware_cmake`).
    pub pkg_name: String,
    /// Absolute path to the package source directory.
    pub src_dir: PathBuf,
    /// Per-package build directory (e.g. `build/autoware_cmake/`).
    pub build_dir: PathBuf,
    /// Per-package install prefix (e.g. `install/autoware_cmake/`).
    pub install_dir: PathBuf,
    /// Per-package log directory (e.g. `log/<timestamp>/autoware_cmake/`).
    pub log_dir: PathBuf,
    /// Environment variables to set for child processes.
    /// These override (not append to) the inherited environment.
    pub env: HashMap<String, String>,
    /// Build options from the user.
    pub options: BuildOptionsRef,
    /// Global SIGINT flag — checked before spawning each subprocess.
    pub interrupted: &'static AtomicBool,
}

/// The subset of [`super::BuildOptions`] needed by backends, passed by value.
#[derive(Debug, Clone)]
pub struct BuildOptionsRef {
    pub parallel_workers: usize,
    pub cmake_args: Vec<String>,
    pub make_args: Vec<String>,
    pub symlink_install: bool,
    pub test_mode: bool,
    pub dry_run: bool,
}

impl From<&super::BuildOptions> for BuildOptionsRef {
    fn from(opts: &super::BuildOptions) -> Self {
        Self {
            parallel_workers: opts.parallel_workers,
            cmake_args: opts.cmake_args.clone(),
            make_args: opts.make_args.clone(),
            symlink_install: opts.symlink_install,
            test_mode: opts.test_mode,
            dry_run: opts.dry_run,
        }
    }
}

/// Build an ament_cmake package: configure → build → install.
///
/// Returns `Ok(())` on success, or an appropriate [`crate::Error`] on failure.
pub fn build_ament_cmake(ctx: &PackageBuildContext) -> crate::Result<()> {
    // Create build and log directories.
    fs::create_dir_all(&ctx.build_dir)?;
    fs::create_dir_all(&ctx.log_dir)?;

    // Validate: reject user-specified BUILD_TESTING.
    validate_cmake_args(&ctx.options.cmake_args)?;

    // Step 1: cmake configure
    configure(ctx)?;

    // Step 2: make
    build(ctx)?;

    // Step 3: make install
    install(ctx)?;

    Ok(())
}

/// Reject `-DBUILD_TESTING=...` in user cmake_args — we control it via `--test-mode`.
fn validate_cmake_args(cmake_args: &[String]) -> crate::Result<()> {
    for arg in cmake_args {
        let upper = arg.to_uppercase();
        if upper.starts_with("-DBUILD_TESTING=") || upper.starts_with("-DBUILD_TESTING:") {
            return Err(crate::Error::InvalidBuildFlag {
                detail: format!(
                    "BUILD_TESTING is managed by launch-plus (use --test-mode). \
                     Remove '{arg}' from --cmake-args."
                ),
            });
        }
    }
    Ok(())
}

/// Step 1: cmake configure.
fn configure(ctx: &PackageBuildContext) -> crate::Result<()> {
    let build_testing = if ctx.options.test_mode { "ON" } else { "OFF" };

    let mut args = vec![
        format!(
            "-DCMAKE_INSTALL_PREFIX={}",
            ctx.install_dir.to_string_lossy()
        ),
        format!("-DBUILD_TESTING={build_testing}"),
    ];

    // Symlink install: ament_cmake supports this via a cmake variable.
    if ctx.options.symlink_install {
        args.push("-DCMAKE_INSTALL_SYMLINKS=ON".to_string());
    }

    // User cmake args.
    args.extend(ctx.options.cmake_args.iter().cloned());

    // Source directory last.
    args.push(ctx.src_dir.to_string_lossy().into_owned());

    run_command(ctx, "cmake", &args, Some(&ctx.build_dir), "configure")
}

/// Step 2: make (parallel build).
fn build(ctx: &PackageBuildContext) -> crate::Result<()> {
    let mut args = vec![format!("-j{}", ctx.options.parallel_workers)];

    // User make args.
    args.extend(ctx.options.make_args.iter().cloned());

    run_command(ctx, "make", &args, Some(&ctx.build_dir), "build")
}

/// Step 3: make install.
fn install(ctx: &PackageBuildContext) -> crate::Result<()> {
    run_command(
        ctx,
        "make",
        &["install".to_string()],
        Some(&ctx.build_dir),
        "install",
    )
}

/// Run a subprocess with environment overrides, logging, and SIGINT awareness.
///
/// * `step_name` is used for log file naming and error messages (e.g. "configure", "build").
/// * stdout and stderr are tee'd to `<log_dir>/<step_name>_stdout.log` / `_stderr.log`.
/// * If `ctx.options.dry_run`, prints the command and returns without executing.
pub fn run_command(
    ctx: &PackageBuildContext,
    program: &str,
    args: &[String],
    working_dir: Option<&Path>,
    step_name: &str,
) -> crate::Result<()> {
    // Check for interruption before spawning.
    if ctx.interrupted.load(Ordering::SeqCst) {
        return Err(crate::Error::BuildInterrupted);
    }

    let cmd_str = format!(
        "{} {}",
        program,
        args.iter()
            .map(|a| shell_escape(a))
            .collect::<Vec<_>>()
            .join(" ")
    );

    if ctx.options.dry_run {
        let dir_str = working_dir
            .map(|d| d.to_string_lossy().into_owned())
            .unwrap_or_else(|| ".".to_string());
        println!("[{}] (in {}) {}", ctx.pkg_name, dir_str, cmd_str);
        return Ok(());
    }

    tracing::debug!("[{}] {}: {}", ctx.pkg_name, step_name, cmd_str);

    let mut cmd = Command::new(program);
    cmd.args(args);

    if let Some(dir) = working_dir {
        cmd.current_dir(dir);
    }

    // Merge environment: start with inherited env, override with computed vars.
    for (key, val) in &ctx.env {
        cmd.env(key, val);
    }

    // Capture stdout and stderr via pipes for logging.
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| crate::Error::BuildFailed {
        package: ctx.pkg_name.clone(),
        detail: format!("{step_name}: failed to spawn '{program}': {e}"),
    })?;

    // Tee stdout/stderr to log files in background threads.
    let stdout_log = ctx.log_dir.join(format!("{step_name}_stdout.log"));
    let stderr_log = ctx.log_dir.join(format!("{step_name}_stderr.log"));

    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();

    let pkg_name = ctx.pkg_name.clone();
    let step = step_name.to_string();

    // Use scoped threads so we don't need 'static lifetimes.
    let status = std::thread::scope(|s| {
        // Stdout reader thread.
        let stdout_handle = stdout_pipe.map(|pipe| s.spawn(move || tee_to_file(pipe, &stdout_log)));

        // Stderr reader thread — tee to both log file and terminal.
        let stderr_handle =
            stderr_pipe.map(|pipe| s.spawn(move || tee_to_file_and_stderr(pipe, &stderr_log)));

        let status = child.wait().map_err(|e| crate::Error::BuildFailed {
            package: pkg_name.clone(),
            detail: format!("{step}: failed to wait for '{program}': {e}"),
        });

        // Wait for tee threads.
        if let Some(h) = stdout_handle {
            let _ = h.join();
        }
        if let Some(h) = stderr_handle {
            let _ = h.join();
        }

        status
    })?;

    // Check for interruption after child exits.
    if ctx.interrupted.load(Ordering::SeqCst) {
        return Err(crate::Error::BuildInterrupted);
    }

    if !status.success() {
        let code = status
            .code()
            .map(|c| c.to_string())
            .unwrap_or_else(|| "signal".to_string());
        return Err(crate::Error::BuildFailed {
            package: ctx.pkg_name.clone(),
            detail: format!(
                "{step_name} failed (exit code {code}). \
                 See logs: {}",
                ctx.log_dir.to_string_lossy()
            ),
        });
    }

    Ok(())
}

/// Read from `reader` line-by-line, writing each line to `log_path`.
fn tee_to_file(reader: impl std::io::Read, log_path: &Path) {
    let file = match fs::File::create(log_path) {
        Ok(f) => f,
        Err(e) => {
            tracing::warn!("Failed to create log file {}: {e}", log_path.display());
            return;
        }
    };
    let mut writer = std::io::BufWriter::new(file);
    let buf = BufReader::new(reader);
    for line in buf.lines() {
        match line {
            Ok(line) => {
                use std::io::Write;
                let _ = writeln!(writer, "{line}");
            }
            Err(_) => break,
        }
    }
}

/// Read from `reader` line-by-line, writing to `log_path` and forwarding
/// every line to stderr so build errors/warnings are immediately visible.
fn tee_to_file_and_stderr(reader: impl std::io::Read, log_path: &Path) {
    let file = match fs::File::create(log_path) {
        Ok(f) => f,
        Err(e) => {
            tracing::warn!("Failed to create log file {}: {e}", log_path.display());
            return;
        }
    };
    let mut writer = std::io::BufWriter::new(file);
    let buf = BufReader::new(reader);
    for line in buf.lines() {
        match line {
            Ok(line) => {
                use std::io::Write;
                let _ = writeln!(writer, "{line}");
                eprintln!("{line}");
            }
            Err(_) => break,
        }
    }
}

/// Minimal shell escaping for display purposes (not security-critical).
fn shell_escape(s: &str) -> String {
    if s.contains(' ') || s.contains('\'') || s.contains('"') || s.contains('\\') || s.is_empty() {
        format!("'{}'", s.replace('\'', "'\\''"))
    } else {
        s.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_cmake_args_rejects_build_testing() {
        let args = vec!["-DBUILD_TESTING=ON".to_string()];
        let result = validate_cmake_args(&args);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("BUILD_TESTING"));
    }

    #[test]
    fn test_validate_cmake_args_rejects_case_insensitive() {
        let args = vec!["-Dbuild_testing=OFF".to_string()];
        let result = validate_cmake_args(&args);
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_cmake_args_rejects_typed() {
        // cmake typed variable: -DBUILD_TESTING:BOOL=ON
        let args = vec!["-DBUILD_TESTING:BOOL=ON".to_string()];
        let result = validate_cmake_args(&args);
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_cmake_args_allows_other() {
        let args = vec![
            "-DCMAKE_BUILD_TYPE=Release".to_string(),
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON".to_string(),
        ];
        let result = validate_cmake_args(&args);
        assert!(result.is_ok());
    }

    #[test]
    fn test_shell_escape() {
        assert_eq!(shell_escape("simple"), "simple");
        assert_eq!(shell_escape("has space"), "'has space'");
        assert_eq!(shell_escape(""), "''");
        assert_eq!(shell_escape("it's"), "'it'\\''s'");
    }

    #[test]
    fn test_dry_run_does_not_execute() {
        use tempfile::TempDir;

        let tmp = TempDir::new().unwrap();
        let ctx = PackageBuildContext {
            pkg_name: "test_pkg".to_string(),
            src_dir: tmp.path().join("src"),
            build_dir: tmp.path().join("build"),
            install_dir: tmp.path().join("install"),
            log_dir: tmp.path().join("log"),
            env: HashMap::new(),
            options: BuildOptionsRef {
                parallel_workers: 4,
                cmake_args: vec![],
                make_args: vec![],
                symlink_install: false,
                test_mode: false,
                dry_run: true,
            },
            interrupted: &DUMMY_INTERRUPTED,
        };

        // Should succeed without actually running cmake (which isn't installed in test env).
        let result = build_ament_cmake(&ctx);
        assert!(result.is_ok());
    }

    static DUMMY_INTERRUPTED: AtomicBool = AtomicBool::new(false);

    #[test]
    fn test_interrupted_prevents_execution() {
        use tempfile::TempDir;

        static TEST_INTERRUPTED: AtomicBool = AtomicBool::new(true);

        let tmp = TempDir::new().unwrap();
        let ctx = PackageBuildContext {
            pkg_name: "test_pkg".to_string(),
            src_dir: tmp.path().join("src"),
            build_dir: tmp.path().join("build"),
            install_dir: tmp.path().join("install"),
            log_dir: tmp.path().join("log"),
            env: HashMap::new(),
            options: BuildOptionsRef {
                parallel_workers: 4,
                cmake_args: vec![],
                make_args: vec![],
                symlink_install: false,
                test_mode: false,
                dry_run: false,
            },
            interrupted: &TEST_INTERRUPTED,
        };

        let result = build_ament_cmake(&ctx);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("interrupted"));
    }
}
