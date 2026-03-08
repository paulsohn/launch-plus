//! launch-plus CLI: Bazel-like build and run system for ROS 2

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use launch_plus_core::fetcher::{fetch_packages, FetchOptions, WorkspaceState};
use launch_plus_core::indexer::{
    blobless_clone, discover_packages, generate_lockfile, parse_lockfile, parse_repos,
    resolve_version_local, serialize_lockfile, Lockfile,
};
use launch_plus_core::orchestrator::{resolve_launch_recursive, ResolveWorkflowOptions};
use std::fs;
use std::path::Path;

/// Bazel-like build and run system for ROS 2
#[derive(Parser)]
#[command(name = "launch-plus")]
#[command(author, version, about, long_about = None)]
struct Cli {
    /// Verbose output
    #[arg(short, long, global = true)]
    verbose: bool,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Generate lockfile from .repos files
    #[command(override_usage = "launch-plus index [OPTIONS] [FILES]...")]
    Index {
        /// .repos files to process (default: manifest.repos)
        #[arg(default_value = "manifest.repos")]
        files: Vec<String>,

        /// Append to existing lockfile instead of overwriting
        #[arg(long)]
        append: bool,

        /// Output lockfile path (default: manifest.lock.repos)
        #[arg(short, long, alias = "lockfile")]
        output: Option<String>,

        /// Source directory for cloning repositories
        /// Repos are cloned to <src>/<workspace_path> and reused on subsequent runs
        #[arg(long, default_value = "src")]
        src: String,

        /// Disable recursive cloning of submodules
        /// By default, submodules are cloned recursively with --shallow-submodules
        #[arg(long)]
        no_recurse_submodules: bool,

        /// Verify lockfile consistency without cloning or making HTTP requests
        /// Exit codes: 0=consistent, 1=mismatch, 2=missing repo in lockfile
        #[arg(long)]
        verify: bool,
    },

    /// Update lockfile with latest SHAs by re-resolving refs
    #[command(override_usage = "launch-plus update [OPTIONS] [REPOS]...")]
    Update {
        /// Specific workspace paths to update (default: all repos with a ref)
        repos: Vec<String>,

        /// Input lockfile path (default: manifest.lock.repos)
        #[arg(short, long, alias = "lockfile", default_value = "manifest.lock.repos")]
        input: String,

        /// Output lockfile path (default: same as input)
        #[arg(short, long, alias = "output-lockfile")]
        output: Option<String>,

        /// Source directory for local clones (default: src)
        #[arg(long, default_value = "src")]
        src: String,

        /// Show what would change without updating
        #[arg(long)]
        diff: bool,
    },

    /// Fetch packages from lockfile using sparse-checkout
    #[command(override_usage = "launch-plus fetch [OPTIONS] <PACKAGES>...")]
    Fetch {
        /// Package names to fetch
        #[arg(required = true)]
        packages: Vec<String>,

        /// Lockfile path (default: manifest.lock.repos)
        #[arg(short, long, default_value = "manifest.lock.repos")]
        lockfile: String,

        /// Fetch directory (default: src)
        #[arg(long, default_value = "src")]
        src: String,

        /// Disable recursive submodule fetching
        #[arg(long)]
        no_recurse_submodules: bool,

        /// Use shallow clone (depth=1) when fetching new repositories.
        /// Off by default; useful in CI where clone history is not needed.
        /// Has no effect on repositories already cloned.
        #[arg(long)]
        shallow: bool,
    },

    /// Resolve and flatten launch file (no build)
    ///
    /// Recursively resolves launch file dependencies, fetching packages on demand
    /// via sparse-checkout. Outputs the resolved launch XML to stdout.
    ///
    /// Launch arguments are passed as positional `name:=value` pairs after the launcher.
    #[command(override_usage = "launch-plus resolve [OPTIONS] <PACKAGE> <LAUNCHER> [ARG]...")]
    Resolve {
        /// Package name
        package: String,

        /// Launch file name
        launcher: String,

        /// Launch arguments in name:=value format
        #[arg(value_name = "ARG")]
        args: Vec<String>,

        /// Lockfile path (default: manifest.lock.repos)
        #[arg(short, long, default_value = "manifest.lock.repos")]
        lockfile: String,

        /// Source directory for sparse-checkout (default: src)
        #[arg(long, default_value = "src")]
        src: String,

        /// Print dependency report to stderr in addition to resolved XML
        #[arg(long)]
        report: bool,

        /// Allow child launch files to inherit args from parent files without explicit
        /// forwarding, emulating ROS 2's global LaunchConfiguration context.
        ///
        /// By default (strict mode) every arg used in an included file must be declared
        /// with <arg name="..."/> in that file and explicitly passed via
        /// <arg name="..." value="$(var ...)"/> in the <include> tag.  Strict mode makes
        /// each launch file self-describing and composable.
        ///
        /// This flag disables that check.  Use it only for legacy launch files that cannot
        /// be refactored.  The flag name is intentionally verbose to encourage fixing the
        /// root cause instead.
        #[arg(long)]
        allow_global_arg_cascade: bool,

        /// Apply default="..." values from <arg> declarations when an arg is not supplied.
        ///
        /// By default (strict mode) every arg must be provided explicitly on the command
        /// line.  Missing args surface as "undefined variable" errors, making hidden
        /// dependencies visible.  This prevents the antipattern of silently relying on
        /// a file-author's default instead of configuring the system properly.
        ///
        /// Use this flag only for legacy launch files where supplying every arg is
        /// impractical.  The flag name is intentionally verbose.
        #[arg(long)]
        apply_launch_arg_defaults: bool,

        /// Preview mode: resolve against source workspace instead of install paths.
        ///
        /// Without this flag, resolution requires packages to be installed via
        /// `colcon build` and available on AMENT_PREFIX_PATH.  Use this flag to
        /// resolve against the source workspace before building.
        ///
        /// The resolved XML carries a `<!-- PREVIEW -->` header to signal that it was
        /// produced from source paths and may not match the final installed layout.
        /// Use `semantic_eq` to verify that a preview resolution matches a post-build
        /// resolution node-for-node before launching.
        #[arg(long)]
        preview: bool,

        /// Expand `$(find-pkg-share ...)` tokens in the output to absolute AMENT install
        /// paths.
        ///
        /// Preview mode normally emits portable `$(find-pkg-share pkg)/...` paths.  With
        /// this flag every such token is expanded using the current AMENT_PREFIX_PATH, so
        /// the output is directly comparable (`diff`) with a non-preview (post-build)
        /// resolution.
        ///
        /// Requires `--preview`.  Source the build's `install/setup.bash` before running
        /// so that AMENT_PREFIX_PATH points to the installed packages.
        #[arg(long, requires = "preview")]
        expand_paths: bool,

        /// Allow raw filesystem paths in <include file=...> and <param from=...> in preview
        /// mode instead of requiring $(find-pkg-share ...) substitutions.
        ///
        /// By default (strict mode) any absolute path passed to an include source or param
        /// file in preview mode is an error — such paths are machine-specific and break
        /// portability.  With this flag the check is relaxed to a warning.
        ///
        /// This flag is intentionally verbose to discourage hardcoding machine-specific paths.
        /// Prefer fixing the launch files to use $(find-pkg-share ...) instead.
        #[arg(long)]
        allow_including_unportable_path: bool,

        /// Remove source-boundary <group> wrappers from the resolved output.
        ///
        /// By default each include-file boundary is represented by a `<group>` element
        /// that wraps the nodes originating from that file.  This flag suppresses those
        /// wrapping groups, emitting nodes flat at the `<launch>` level.
        ///
        /// `<!-- source: pkg://... -->` comments are still emitted for traceability.
        ///
        /// `<group>` elements that contain a `<push-ros-namespace>` are preserved so
        /// namespace semantics are not broken.  Combine with `--flatten-namespaces` to
        /// inline those namespaces too, producing a completely group-free output.
        #[arg(long)]
        flatten: bool,

        /// Inline namespace stacks directly onto each <node> element instead of
        /// preserving <push-ros-namespace> wrappers.
        ///
        /// By default the resolved XML preserves `<push-ros-namespace namespace="..."/>`
        /// inside `<group>` elements, faithfully representing how namespaces are applied
        /// in the source launch tree.
        ///
        /// With this flag each node's fully composed effective namespace is emitted as a
        /// `namespace=` attribute on the `<node>` element and `<push-ros-namespace>` is
        /// omitted.  Nodes from the same source file are grouped into a single `<group>`
        /// container regardless of their namespace context.  The resulting XML is
        /// semantically equivalent to the default output at `ros2 launch` time.
        ///
        /// Useful for flattening deeply nested namespace hierarchies into a more readable,
        /// single-file layout.
        #[arg(long)]
        flatten_namespaces: bool,

        /// Emit `<!-- arg name="..." value="..." -->` comments at each include boundary.
        ///
        /// These comments show the explicit arguments forwarded to each included launch
        /// file, which can be helpful for tracing how arguments propagate through a deeply
        /// nested launch tree.  Off by default to keep the output concise.
        #[arg(long)]
        show_args: bool,

        /// Allow OpaqueFunction bodies in Python launch files to open files via portable
        /// `$(find-pkg-share pkg)/...` paths, resolving them to actual filesystem paths.
        ///
        /// By default (strict mode) any `open()` or `os.path.*` call inside an
        /// OpaqueFunction that receives a portable path is recorded as an error and
        /// returns stub data.  This surfaces launch files that perform file I/O during
        /// dependency resolution — an antipattern that makes static analysis dependent
        /// on config file contents.
        ///
        /// Use this flag for legacy launch files that call
        /// `open(LaunchConfiguration("param_file"))` where the arg carries a
        /// `$(find-pkg-share ...)` path.  The flag name is intentionally verbose.
        #[arg(long)]
        apply_opaque_file_access: bool,

        /// Expand `<param from="file.yaml"/>` entries inline as individual
        /// `<param name="..." value="..."/>` elements.
        ///
        /// By default resolved XML preserves `<param from="..."/>` references.  With
        /// this flag the YAML file is read and each `ros__parameters` key is emitted as
        /// a separate `<param>` element, making all parameter values visible without
        /// opening the YAML files.
        ///
        /// All standard ROS 2 parameter YAML layouts are supported:
        ///   - bare `ros__parameters: ...`
        ///   - `/**:\n  ros__parameters: ...`  (Autoware wildcard convention)
        ///   - `/ns:\n  node_name:\n    ros__parameters: ...`  (general ROS 2)
        #[arg(long)]
        inline_params: bool,

        /// Automatically install packages not in the lockfile via rosdep.
        ///
        /// When a required package is not in the lockfile (e.g. a ROS buildfarm package
        /// like `rosbridge_server`), the resolver first checks AMENT_PREFIX_PATH.  If
        /// found there it is used directly.  If not found and this flag is set, the
        /// resolver runs `rosdep install --from-keys <package>` to install it, then
        /// retries.
        ///
        /// Requires ROS_DISTRO to be set (source /opt/ros/<distro>/setup.bash).
        #[arg(long)]
        rosdep: bool,

        /// Show all warnings including commonly-suppressed categories.
        ///
        /// By default, pedantic warnings that are noisy in practice are suppressed
        /// and only their count is shown.  Currently suppressed categories:
        ///   - `$(env VAR)` without a fallback (e.g. HOME is always set)
        ///   - unportable param source paths (e.g. ~/autoware_data convention)
        ///
        /// With this flag every warning is printed.
        #[arg(long)]
        warn_all: bool,

        /// Reset every repository to the pinned lockfile SHA before resolving.
        /// Local modifications are discarded.  Guarantees reproducible output.
        /// Exactly one of --clean or --dirty must be specified.
        #[arg(short = 'c', long, conflicts_with = "dirty")]
        clean: bool,

        /// Use the current on-disk state without any git operations.
        /// Only fetches repositories that are completely missing from disk.
        /// Exactly one of --clean or --dirty must be specified.
        #[arg(short = 'd', long, conflicts_with = "clean")]
        dirty: bool,

        /// Use shallow clone (depth=1) when fetching new repositories.
        /// Off by default; useful in CI where clone history is not needed.
        /// Has no effect on repositories already cloned.
        #[arg(long)]
        shallow: bool,
    },

    /// Fetch, build, and launch (full execution)
    Run {
        /// Package name
        package: String,

        /// Launch file name
        launcher: String,
    },

    /// Validate a launch file without producing output (thin alias over resolve).
    ///
    /// Runs the full resolver pipeline and reports errors and warnings to stderr.
    /// Exits 0 when the launch file resolves cleanly, non-zero otherwise.
    ///
    /// Equivalent to `resolve` with XML output suppressed and strict-by-default
    /// error policy.  All `resolve` flags are accepted.
    ///
    /// Launch arguments are passed as positional `name:=value` pairs.
    #[command(override_usage = "launch-plus check [OPTIONS] <PACKAGE> <LAUNCHER> [ARG]...")]
    Check {
        /// Package name
        package: String,

        /// Launch file name
        launcher: String,

        /// Launch arguments in name:=value format
        #[arg(value_name = "ARG")]
        args: Vec<String>,

        /// Lockfile path (default: manifest.lock.repos)
        #[arg(short, long, default_value = "manifest.lock.repos")]
        lockfile: String,

        /// Source directory for sparse-checkout (default: src)
        #[arg(long, default_value = "src")]
        src: String,

        /// Allow child launch files to inherit args from parent without explicit forwarding.
        /// See `resolve --allow-global-arg-cascade` for details.
        #[arg(long)]
        allow_global_arg_cascade: bool,

        /// Apply default="..." values from <arg> declarations when an arg is not supplied.
        /// See `resolve --apply-launch-arg-defaults` for details.
        #[arg(long)]
        apply_launch_arg_defaults: bool,

        /// Resolve against source workspace instead of install paths.
        /// See `resolve --preview` for details.
        #[arg(long)]
        preview: bool,

        /// Allow raw filesystem paths in <include> and <param from> in preview mode.
        /// See `resolve --allow-including-unportable-path` for details.
        #[arg(long)]
        allow_including_unportable_path: bool,

        /// Allow OpaqueFunction bodies to open files via portable paths.
        /// See `resolve --apply-opaque-file-access` for details.
        #[arg(long)]
        apply_opaque_file_access: bool,

        /// Automatically install packages not in the lockfile via rosdep.
        /// See `resolve --rosdep` for details.
        #[arg(long)]
        rosdep: bool,

        /// Treat warnings as errors (exit non-zero if any warning is emitted).
        ///
        /// By default `check` exits non-zero only on resolution errors.
        /// With `--strict` any warning (e.g. OpaqueFunction failures, undefined
        /// variables in optional paths) is also treated as a failure.
        #[arg(long)]
        strict: bool,

        /// Show all warnings including commonly-suppressed categories.
        /// See `resolve --warn-all` for details.
        #[arg(long)]
        warn_all: bool,

        /// Reset every repository to the pinned lockfile SHA before checking.
        /// Local modifications are discarded.  Guarantees reproducible output.
        /// Exactly one of --clean or --dirty must be specified.
        #[arg(short = 'c', long, conflicts_with = "dirty")]
        clean: bool,

        /// Use the current on-disk state without any git operations.
        /// Only fetches repositories that are completely missing from disk.
        /// Exactly one of --clean or --dirty must be specified.
        #[arg(short = 'd', long, conflicts_with = "clean")]
        dirty: bool,

        /// Use shallow clone (depth=1) when fetching new repositories.
        /// Off by default; useful in CI where clone history is not needed.
        /// Has no effect on repositories already cloned.
        #[arg(long)]
        shallow: bool,
    },

    /// Clean fetched packages
    Clean {
        /// Source directory to clean (default: src)
        #[arg(long, default_value = "src")]
        src: String,

        /// Keep .git directories for faster re-fetch
        #[arg(long)]
        keep_git: bool,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let verbose = cli.verbose;

    // Initialize tracing
    let subscriber = tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(if verbose { "debug" } else { "info" })
        .finish();
    tracing::subscriber::set_global_default(subscriber)?;

    match cli.command {
        Commands::Index {
            files,
            append,
            output,
            src,
            no_recurse_submodules,
            verify,
        } => {
            if verify {
                let exit_code = cmd_verify(&files, output.as_deref())?;
                std::process::exit(exit_code);
            } else {
                cmd_index(&files, append, output.as_deref(), &src, !no_recurse_submodules)?;
            }
        }
        Commands::Update {
            repos,
            input,
            output,
            src,
            diff,
        } => {
            cmd_update(&repos, &input, output.as_deref(), &src, diff)?;
        }
        Commands::Fetch {
            packages,
            lockfile,
            src,
            no_recurse_submodules,
            shallow,
        } => {
            cmd_fetch(&packages, &lockfile, &src, !no_recurse_submodules, shallow)?;
        }
        Commands::Resolve {
            package,
            launcher,
            args,
            lockfile,
            src,
            report,
            allow_global_arg_cascade,
            apply_launch_arg_defaults,
            preview,
            expand_paths,
            allow_including_unportable_path,
            apply_opaque_file_access,
            inline_params,
            flatten,
            flatten_namespaces,
            show_args,
            rosdep,
            warn_all,
            clean,
            dirty,
            shallow,
        } => {
            let workspace_state = parse_workspace_state(clean, dirty)?;
            let initial_args = parse_launch_args(&args)?;
            let workflow_options = ResolveWorkflowOptions {
                global_arg_cascade: allow_global_arg_cascade,
                apply_arg_defaults: apply_launch_arg_defaults,
                preview,
                allow_unportable_paths: allow_including_unportable_path,
                apply_opaque_file_access,
                rosdep_fallback: rosdep,
                inline_params,
            };
            cmd_resolve(
                &package,
                &launcher,
                initial_args,
                &lockfile,
                &src,
                report,
                preview,
                expand_paths,
                flatten,
                flatten_namespaces,
                show_args,
                false,  // suppress_xml — resolve always emits XML
                false,  // strict — resolve exits non-zero only on errors
                warn_all,
                workspace_state,
                workflow_options,
                shallow,
            )?;
        }
        Commands::Run { package, launcher } => {
            tracing::info!("Running {}/{}", package, launcher);
            println!("launch-plus run: not yet implemented");
        }
        Commands::Check {
            package,
            launcher,
            args,
            lockfile,
            src,
            allow_global_arg_cascade,
            apply_launch_arg_defaults,
            preview,
            allow_including_unportable_path,
            apply_opaque_file_access,
            rosdep,
            strict,
            warn_all,
            clean,
            dirty,
            shallow,
        } => {
            let workspace_state = parse_workspace_state(clean, dirty)?;
            let initial_args = parse_launch_args(&args)?;
            let workflow_options = ResolveWorkflowOptions {
                global_arg_cascade: allow_global_arg_cascade,
                apply_arg_defaults: apply_launch_arg_defaults,
                preview,
                allow_unportable_paths: allow_including_unportable_path,
                apply_opaque_file_access,
                rosdep_fallback: rosdep,
                inline_params: false,  // check suppresses XML anyway
            };
            cmd_resolve(
                &package,
                &launcher,
                initial_args,
                &lockfile,
                &src,
                false,   // report
                preview,
                false,   // expand_paths — not applicable for check
                false,   // flatten
                false,   // flatten_namespaces
                false,   // show_args — irrelevant, XML is suppressed
                true,    // suppress_xml — check never writes resolved XML to stdout
                strict,
                warn_all,
                workspace_state,
                workflow_options,
                shallow,
            )?;
        }
        Commands::Clean { src, keep_git } => {
            cmd_clean(&src, keep_git)?;
        }
    }

    Ok(())
}

/// Execute the index command: generate lockfile from .repos files
fn cmd_index(
    files: &[String],
    append: bool,
    output: Option<&str>,
    src_dir: &str,
    recurse_submodules: bool,
) -> Result<()> {
    let output_path = output.unwrap_or("manifest.lock.repos");
    let src_path = Path::new(src_dir);

    tracing::info!("Using source directory: {}", src_path.display());

    // Start with existing lockfile if appending
    let mut lockfile = if append && Path::new(output_path).exists() {
        let content = fs::read_to_string(output_path)
            .with_context(|| format!("failed to read existing lockfile: {output_path}"))?;
        parse_lockfile(&content).with_context(|| "failed to parse existing lockfile")?
    } else {
        Lockfile::new()
    };

    // Process each .repos file
    for file in files {
        tracing::info!("Processing {}", file);

        let content = fs::read_to_string(file)
            .with_context(|| format!("failed to read .repos file: {file}"))?;

        let repos = parse_repos(&content).with_context(|| format!("failed to parse {file}"))?;

        tracing::info!(
            "Found {} repositories in {}",
            repos.repositories.len(),
            file
        );

        // Atomic update: remove old entries for repos we're about to process
        // This handles package migrations between repositories correctly
        for workspace_path in repos.repositories.keys() {
            if lockfile.repositories.contains_key(workspace_path) {
                tracing::debug!(
                    "Removing old entry for {} before re-indexing",
                    workspace_path
                );
                lockfile.remove_repo(workspace_path);
            }
        }

        // Generate lockfile entries for this .repos file
        let file_lockfile = generate_lockfile(&repos, Some(src_path), recurse_submodules)
            .with_context(|| format!("failed to index {file}"))?;

        // Merge into main lockfile (no conflicts possible after removal)
        for (key, repo) in file_lockfile.repositories {
            lockfile.repositories.insert(key, repo);
        }

        for (name, pkg) in file_lockfile.packages {
            // Check for duplicate packages from OTHER repos (not the ones we just removed)
            if let Some(existing) = lockfile.packages.get(&name) {
                // Only error if it's from a different repo we didn't just process
                if !repos.repositories.contains_key(&existing.repo) {
                    tracing::warn!(
                        "Package {} already exists in {}, skipping from {}",
                        name,
                        existing.repo,
                        pkg.repo
                    );
                    continue;
                }
            }
            lockfile.packages.insert(name, pkg);
        }
    }

    // Write lockfile
    let yaml = serialize_lockfile(&lockfile).with_context(|| "failed to serialize lockfile")?;
    fs::write(output_path, &yaml)
        .with_context(|| format!("failed to write lockfile: {output_path}"))?;

    tracing::info!(
        "Wrote lockfile with {} repositories and {} packages to {}",
        lockfile.repositories.len(),
        lockfile.packages.len(),
        output_path
    );

    println!(
        "Generated {} with {} repositories and {} packages",
        output_path,
        lockfile.repositories.len(),
        lockfile.packages.len()
    );

    Ok(())
}

/// Verify lockfile consistency with .repos files (no HTTP requests, no cloning)
///
/// Returns exit code:
/// - 0: All repositories consistent
/// - 1: Mismatch between .repos and lockfile
/// - 2: Repository in .repos missing from lockfile
fn cmd_verify(files: &[String], output: Option<&str>) -> Result<i32> {
    let lockfile_path = output.unwrap_or("manifest.lock.repos");

    // Read lockfile
    if !Path::new(lockfile_path).exists() {
        println!("Error: Lockfile not found: {}", lockfile_path);
        println!("Run 'launch-plus index' to generate it.");
        return Ok(2);
    }

    let lockfile_content = fs::read_to_string(lockfile_path)
        .with_context(|| format!("failed to read lockfile: {lockfile_path}"))?;
    let lockfile = parse_lockfile(&lockfile_content)
        .with_context(|| format!("failed to parse lockfile: {lockfile_path}"))?;

    println!("Verifying against {}...", lockfile_path);

    let mut all_ok = true;
    let mut missing = false;
    let mut total_repos = 0;

    // Check each .repos file
    for file in files {
        let content = fs::read_to_string(file)
            .with_context(|| format!("failed to read .repos file: {file}"))?;
        let repos = parse_repos(&content).with_context(|| format!("failed to parse {file}"))?;

        for (workspace_path, entry) in &repos.repositories {
            total_repos += 1;

            // Check if repo exists in lockfile
            let Some(lock_entry) = lockfile.repositories.get(workspace_path) else {
                println!("  {}: MISSING from lockfile", workspace_path);
                missing = true;
                continue;
            };

            // Determine what to compare based on version format
            let is_sha = entry.version.len() == 40
                && entry.version.chars().all(|c| c.is_ascii_hexdigit());

            let (expected, actual, label) = if is_sha {
                // Version is SHA: compare directly with lockfile version
                (&entry.version, &lock_entry.version, "sha")
            } else {
                // Version is tag/branch: compare with lockfile ref
                let actual = lock_entry.version_ref.as_ref().unwrap_or(&lock_entry.version);
                (&entry.version, actual, "ref")
            };

            if expected == actual {
                println!("  {}: OK ({}={})", workspace_path, label, expected);
            } else {
                println!("  {}: MISMATCH", workspace_path);
                println!("    .repos {}: {}", label, expected);
                println!("    lockfile {}: {}", label, actual);
                all_ok = false;
            }
        }
    }

    println!();

    if missing {
        println!(
            "Error: {} has repositories not in lockfile.",
            files.join(", ")
        );
        println!("Run 'launch-plus index' to regenerate.");
        return Ok(2);
    }

    if !all_ok {
        println!("Error: Lockfile inconsistent with .repos files.");
        println!("Run 'launch-plus index' to regenerate.");
        return Ok(1);
    }

    println!("All {} repositories consistent.", total_repos);
    Ok(0)
}

/// Execute the update command: update lockfile with latest SHAs
fn cmd_update(
    filter: &[String],
    input: &str,
    output: Option<&str>,
    src_dir: &str,
    diff_only: bool,
) -> Result<()> {
    let output_path = output.unwrap_or(input);
    let src_path = Path::new(src_dir);

    // Read existing lockfile
    let content =
        fs::read_to_string(input).with_context(|| format!("failed to read lockfile: {input}"))?;
    let mut lockfile =
        parse_lockfile(&content).with_context(|| format!("failed to parse lockfile: {input}"))?;

    tracing::info!(
        "Loaded lockfile with {} repositories",
        lockfile.repositories.len()
    );

    if !filter.is_empty() {
        tracing::info!("Updating only: {:?}", filter);
    }

    let mut updates: Vec<(String, String, String)> = Vec::new(); // (workspace_path, old_sha, new_sha)
    let mut errors: Vec<(String, String)> = Vec::new(); // (workspace_path, error)

    // Check each repository for updates by re-resolving the ref
    for (workspace_path, repo) in &lockfile.repositories {
        // Apply selective filter
        if !filter.is_empty() && !filter.iter().any(|f| f == workspace_path) {
            continue;
        }

        // Skip repos without a ref (pinned to exact SHA, nothing to update)
        let Some(ref version_ref) = repo.version_ref else {
            tracing::debug!(
                "Skipping {} (pinned to SHA, no ref to update)",
                workspace_path,
            );
            continue;
        };

        tracing::debug!(
            "Checking {} (ref={}, current={})",
            workspace_path,
            version_ref,
            &repo.version[..8.min(repo.version.len())]
        );

        // Resolve via local fetch; blobless-clone first if no local clone exists.
        let repo_dir = src_path.join(workspace_path);
        let resolve_result = if !repo_dir.join(".git").exists() {
            tracing::debug!("No local clone for {}, blobless-cloning first", workspace_path);
            blobless_clone(&repo.url, &repo_dir)
                .and_then(|()| resolve_version_local(&repo_dir, version_ref))
        } else {
            resolve_version_local(&repo_dir, version_ref)
        };

        match resolve_result {
            Ok(new_sha) => {
                if new_sha != repo.version {
                    updates.push((workspace_path.clone(), repo.version.clone(), new_sha));
                }
            }
            Err(e) => {
                errors.push((workspace_path.clone(), e.to_string()));
            }
        }
    }

    // Report errors
    for (workspace_path, error) in &errors {
        tracing::warn!("Failed to resolve {}: {}", workspace_path, error);
    }

    // Report updates
    if updates.is_empty() {
        println!("All repositories are up to date.");
        return Ok(());
    }

    println!("Found {} updates:", updates.len());
    for (workspace_path, old_sha, new_sha) in &updates {
        println!(
            "  {} {} -> {}",
            workspace_path,
            &old_sha[..8.min(old_sha.len())],
            &new_sha[..8.min(new_sha.len())]
        );
    }

    if diff_only {
        println!("\nRun without --diff to apply updates.");
        return Ok(());
    }

    // Apply updates
    println!("\nApplying updates...");
    for (workspace_path, _old_sha, new_sha) in &updates {
        let repo = lockfile.repositories.get_mut(workspace_path).unwrap();
        let old_packages = repo.packages.clone();

        // Update SHA
        repo.version = new_sha.clone();

        // Re-scan packages if repo exists locally
        let repo_dir = src_path.join(workspace_path);
        if repo_dir.exists() {
            tracing::debug!("Re-scanning packages in {}", workspace_path);
            match discover_packages(&repo.url, new_sha, Some(&repo_dir), true) {
                Ok(packages) => {
                    // Remove old packages from lockfile
                    for pkg_name in &old_packages {
                        lockfile.packages.remove(pkg_name);
                    }

                    // Add new packages
                    let new_package_names: Vec<String> =
                        packages.iter().map(|p| p.name.clone()).collect();
                    repo.packages = new_package_names;

                    for pkg in packages {
                        lockfile.packages.insert(
                            pkg.name,
                            launch_plus_core::indexer::PackageLock {
                                repo: workspace_path.clone(),
                                path: pkg.path,
                                dependencies: pkg.dependencies,
                            },
                        );
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        "Failed to re-scan packages in {}: {}. Keeping old package list.",
                        workspace_path,
                        e
                    );
                }
            }
        }
    }

    // Write updated lockfile
    let yaml = serialize_lockfile(&lockfile).with_context(|| "failed to serialize lockfile")?;
    fs::write(output_path, &yaml)
        .with_context(|| format!("failed to write lockfile: {output_path}"))?;

    println!(
        "Updated {} with {} repositories and {} packages",
        output_path,
        lockfile.repositories.len(),
        lockfile.packages.len()
    );

    Ok(())
}

/// Execute the fetch command: fetch packages from lockfile using sparse-checkout
fn cmd_fetch(
    packages: &[String],
    lockfile_path: &str,
    fetch_dir: &str,
    recurse_submodules: bool,
    shallow: bool,
) -> Result<()> {
    // Read lockfile
    let content = fs::read_to_string(lockfile_path)
        .with_context(|| format!("failed to read lockfile: {lockfile_path}"))?;
    let lockfile = parse_lockfile(&content)
        .with_context(|| format!("failed to parse lockfile: {lockfile_path}"))?;

    let fetch_path = Path::new(fetch_dir);
    let options = FetchOptions {
        recurse_submodules,
        shallow,
        workspace_state: WorkspaceState::Clean,  // fetch always syncs to lockfile SHA
    };

    tracing::info!("Fetching {} packages into {}", packages.len(), fetch_dir);

    // Fetch packages
    let fetched = fetch_packages(packages, &lockfile, fetch_path, &options)
        .with_context(|| "failed to fetch packages")?;

    // Report results
    println!("Fetched {} packages:", fetched.len());
    for pkg in &fetched {
        println!("  {} -> {}", pkg.name, pkg.path.display());
    }

    Ok(())
}

/// Parse `name:=value` launch argument strings into a HashMap
fn parse_launch_args(args: &[String]) -> Result<std::collections::HashMap<String, String>> {
    let mut map = std::collections::HashMap::new();
    for arg in args {
        let (name, value) = arg.split_once(":=").with_context(|| {
            format!("invalid launch argument '{arg}': expected 'name:=value' format")
        })?;
        map.insert(name.to_string(), value.to_string());
    }
    Ok(map)
}

/// Validate that exactly one of `--clean` / `--dirty` is set, returning the
/// corresponding [`WorkspaceState`].  Emits a user-friendly error when neither
/// flag is given (clap already prevents both being set via `conflicts_with`).
fn parse_workspace_state(clean: bool, dirty: bool) -> Result<WorkspaceState> {
    match (clean, dirty) {
        (true, false) => Ok(WorkspaceState::Clean),
        (false, true) => Ok(WorkspaceState::Dirty),
        (false, false) => {
            anyhow::bail!(
                "specify how to treat the source workspace:\n  \
                 -c, --clean   reset each repository to the pinned lockfile SHA\n  \
                 -d, --dirty   use the current on-disk state without any git operations"
            );
        }
        (true, true) => unreachable!("clap conflicts_with prevents --clean and --dirty together"),
    }
}

/// Execute the resolve command: recursively resolve launch file dependencies
fn cmd_resolve(
    package: &str,
    launcher: &str,
    initial_args: std::collections::HashMap<String, String>,
    lockfile_path: &str,
    src_dir: &str,
    report: bool,
    preview: bool,
    expand_paths: bool,
    flatten: bool,
    flatten_namespaces: bool,
    show_args: bool,
    suppress_xml: bool,
    strict: bool,
    warn_all: bool,
    workspace_state: WorkspaceState,
    workflow_options: ResolveWorkflowOptions,
    shallow: bool,
) -> Result<()> {
    use launch_plus_core::resolver::render_resolved_xml;

    // Read lockfile
    let content = fs::read_to_string(lockfile_path)
        .with_context(|| format!("failed to read lockfile: {lockfile_path}"))?;
    let lockfile = parse_lockfile(&content)
        .with_context(|| format!("failed to parse lockfile: {lockfile_path}"))?;

    let fetch_path = Path::new(src_dir);
    let options = FetchOptions {
        recurse_submodules: true,
        shallow,
        workspace_state,
    };

    // Resolve with package-level fetching
    let result = resolve_launch_recursive(
        &lockfile,
        package,
        launcher,
        fetch_path,
        &options,
        initial_args,
        &workflow_options,
    )
    .with_context(|| format!("failed to resolve {package}/{launcher}"))?;

    // Resolved XML → stdout (suppressed in check mode)
    if !suppress_xml {
        let mut xml = render_resolved_xml(package, launcher, &result.nodes, flatten_namespaces, flatten, &result.include_args, show_args, &result.initial_args);
        if preview && expand_paths {
            // Expand $(find-pkg-share <pkg>) tokens to absolute AMENT install paths
            // so the output is directly comparable with a non-preview (post-build)
            // resolution.  Requires AMENT_PREFIX_PATH from the build's install tree.
            use launch_plus_core::locator::PackageLocator;
            let mut locator = PackageLocator::new();
            locator.add_ament_from_env();
            xml = expand_portable_paths(&xml, &locator);
        } else if preview {
            // Prepend a preview marker so consumers can distinguish source-path output
            // from post-build install-path output.
            xml.insert_str(0, "<!-- PREVIEW: resolved from source workspace, not install paths -->\n");
        }
        println!("{xml}");
    }

    // Report → stderr (errors and warnings in separate sections)
    let all_errors: Vec<&String> = result.errors.iter().collect();
    let has_diagnostics = !all_errors.is_empty() || !result.warnings.is_empty();

    if !all_errors.is_empty() {
        eprintln!();
        for err in &all_errors {
            eprintln!("[error] {}", err);
        }

        // Show hints only for causes that are still active in the current mode
        let hint_defaults = !workflow_options.apply_arg_defaults;
        let hint_cascade = !workflow_options.global_arg_cascade;
        if all_errors.iter().any(|e| e.contains("undefined variable"))
            && (hint_defaults || hint_cascade)
        {
            eprintln!();
            eprintln!("  hint: possible causes for 'undefined variable' in the current mode:");
            if hint_defaults {
                eprintln!();
                eprintln!("  · Arg default not applied (defaults disabled by default).");
                eprintln!("    → Provide the arg on the command line:  x:=value");
                eprintln!("    → Or: --apply-launch-arg-defaults  (applies <arg default=\"...\">)");
            }
            if hint_cascade {
                eprintln!();
                eprintln!("  · Arg set in a parent file but not explicitly forwarded.");
                eprintln!("    → child file:  add <arg name=\"x\"/>");
                eprintln!("    → include tag: add <arg name=\"x\" value=\"$(var x)\"/>");
                eprintln!("    → Or: --allow-global-arg-cascade  (inherits parent context)");
            }
            eprintln!();
            eprintln!("  Flags are intentionally verbose — prefer fixing the launch files.");
        }
    }

    if !result.warnings.is_empty() {
        eprintln!();
        for w in &result.warnings {
            eprintln!("[warning] {}", w);
        }
    }

    // Infos: shown only with --warn-all, otherwise just the count.
    let info_count = result.infos.len();
    if warn_all && !result.infos.is_empty() {
        eprintln!();
        for info in &result.infos {
            eprintln!("[info] {}", info);
        }
    }

    if has_diagnostics || info_count > 0 {
        eprintln!();
        if info_count > 0 && !warn_all {
            eprintln!(
                "{} error(s), {} warning(s), {} info(s) suppressed (use --warn-all to show).",
                all_errors.len(),
                result.warnings.len(),
                info_count
            );
        } else {
            eprintln!(
                "{} error(s), {} warning(s).",
                all_errors.len(),
                result.warnings.len()
            );
        }
    }

    if report {
        eprintln!();
        eprintln!("=== Resolution Report ===");
        eprintln!("Direct packages:   {}", result.direct_packages.len());
        eprintln!("Packages fetched:  {}", result.fetched_packages.len());
        eprintln!("Launch files:      {}", result.launch_files.len());
        eprintln!("Param files:       {}", result.param_files.len());
        eprintln!("Files parsed:      {}", result.parsed_files.len());
        eprintln!("Nodes resolved:    {}", result.nodes.len());

        if !result.launch_files.is_empty() {
            eprintln!("\nLaunch files:");
            for f in &result.launch_files {
                eprintln!("  {}:{}", f.package, f.share_path.display());
            }
        }
        if !result.param_files.is_empty() {
            eprintln!("\nParam files:");
            for f in &result.param_files {
                eprintln!("  {}:{}", f.package, f.share_path.display());
            }
        }
    }

    // Exit policy:
    //   resolve: exit non-zero when errors exist (any mode).
    //   check:   exit non-zero on errors; with --strict, also on warnings.
    let has_errors = !all_errors.is_empty();
    let has_warnings = !result.warnings.is_empty();
    let should_fail = if suppress_xml {
        // check mode: errors always fail; --strict also fails on warnings.
        has_errors || (strict && has_warnings)
    } else {
        // resolve mode: errors always fail; warnings never fail.
        has_errors
    };
    if should_fail {
        std::process::exit(1);
    }

    Ok(())
}

/// Expand `$(find-pkg-share <pkg>)` tokens in `text` to absolute AMENT install paths.
///
/// Each `$(find-pkg-share <pkg>)` occurrence is resolved via the locator's AMENT prefix
/// entries.  Tokens whose package cannot be found are left unchanged.
fn expand_portable_paths(text: &str, locator: &launch_plus_core::locator::PackageLocator) -> String {
    use std::fmt::Write;
    let token = "$(find-pkg-share ";
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find(token) {
        out.push_str(&rest[..start]);
        let after = &rest[start + token.len()..];
        // Find the matching close paren (handle nesting).
        let mut depth = 1u32;
        let mut close = None;
        for (i, c) in after.char_indices() {
            match c {
                '(' => depth += 1,
                ')' => {
                    depth -= 1;
                    if depth == 0 {
                        close = Some(i);
                        break;
                    }
                }
                _ => {}
            }
        }
        if let Some(close_idx) = close {
            let pkg = after[..close_idx].trim();
            if let Some(share_dir) = locator.locate_install_share(pkg) {
                let _ = write!(out, "{}", share_dir.display());
            } else {
                // Package not in AMENT — keep the token as-is.
                out.push_str(&rest[start..start + token.len() + close_idx + 1]);
            }
            rest = &after[close_idx + 1..];
        } else {
            // Unmatched paren — keep the rest as-is.
            out.push_str(&rest[start..]);
            rest = "";
        }
    }
    out.push_str(rest);
    out
}

/// Execute the clean command: remove fetched packages
fn cmd_clean(src_dir: &str, keep_git: bool) -> Result<()> {
    let src_path = Path::new(src_dir);

    if !src_path.exists() {
        println!("Nothing to clean: {} does not exist", src_dir);
        return Ok(());
    }

    if keep_git {
        // Remove everything except .git directories
        tracing::info!("Cleaning {} (keeping .git directories)", src_dir);
        clean_directory_keep_git(src_path)?;
        println!("Cleaned {} (kept .git directories for faster re-fetch)", src_dir);
    } else {
        // Remove the entire src directory
        tracing::info!("Cleaning {} (removing everything)", src_dir);
        fs::remove_dir_all(src_path)
            .with_context(|| format!("failed to remove {}", src_dir))?;
        println!("Cleaned {}", src_dir);
    }

    Ok(())
}

/// Clean directory contents while preserving .git directories
fn clean_directory_keep_git(dir: &Path) -> Result<()> {
    for entry in fs::read_dir(dir).with_context(|| format!("failed to read {}", dir.display()))? {
        let entry = entry.with_context(|| "failed to read directory entry")?;
        let path = entry.path();
        let file_name = entry.file_name();

        if file_name == ".git" {
            // Skip .git directories
            continue;
        }

        if path.is_dir() {
            // Check if this directory or any subdirectory contains a .git
            if contains_git_dir(&path) {
                // Recursively clean, preserving .git
                clean_directory_keep_git(&path)?;
            } else {
                // No .git anywhere, safe to remove entirely
                fs::remove_dir_all(&path)
                    .with_context(|| format!("failed to remove {}", path.display()))?;
            }
        } else {
            // Regular file, remove it
            fs::remove_file(&path)
                .with_context(|| format!("failed to remove {}", path.display()))?;
        }
    }

    Ok(())
}

/// Check if a directory contains a .git directory (directly or in subdirectories)
fn contains_git_dir(dir: &Path) -> bool {
    if dir.join(".git").exists() {
        return true;
    }

    if let Ok(entries) = fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() && contains_git_dir(&path) {
                return true;
            }
        }
    }

    false
}
