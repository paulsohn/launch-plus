//! Resolve orchestrator: Combines locator, fetcher, and resolver
//!
//! This module implements the fetch-on-demand resolution workflow:
//! 1. Discover package dependency from launch file
//! 2. Sparse-checkout the entire package (if not already fetched)
//! 3. Parse launch files within the package
//! 4. Recursively process dependencies
//!
//! Key design: Sparse-checkout operates at **package level** — each package
//! directory is fetched as a unit when a dependency is discovered.
//!
//! # Usage
//!
//! ```ignore
//! let result = resolve_launch_recursive(
//!     &lockfile,
//!     "autoware_launch",
//!     "autoware.launch.xml",
//!     Path::new("src"),
//!     &FetchOptions::default(),
//! )?;
//!
//! println!("Direct packages: {:?}", result.direct_packages);
//! ```

use crate::fetcher::{FetchOptions, fetch_packages};
use crate::indexer::Lockfile;
use crate::locator::PackageLocator;
use crate::resolver::{
    ComposablePlugin, DependencyKind, FileDependency, IncludeArgContext, LaunchInclude, NodeKind,
    ParsedLaunchFile, ResolveOptions, ResolvedLaunch, ResolvedNode, SubstitutionContext,
    collect_arg_and_var_refs, collect_declared_args, collect_env_without_fallback,
    collect_scoped_false_includes, parse_launch_xml, resolve_launch,
};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tracing::{debug, info, warn};

/// Options controlling the resolution workflow (distinct from [`FetchOptions`]).
#[derive(Debug, Clone, Default)]
pub struct ResolveWorkflowOptions {
    /// Pass the full parent arg context to all included files, emulating ROS 2's global
    /// `LaunchConfiguration` sharing.
    ///
    /// **Default: false (strict mode).**  In strict mode only args explicitly forwarded via
    /// `<arg name="..." value="..."/>` in the `<include>` tag are visible in the child file.
    /// This enforces self-describing launch files and makes dependencies explicit.
    ///
    /// When `true`, the full ancestor context cascades to every descendant, matching legacy
    /// ROS 2 behaviour.  Child files can then use `$(var x)` without declaring `<arg name="x"/>`.
    /// This breaks composability — a file's required inputs are no longer visible from its
    /// own source.  Use only for launch files that cannot be refactored; prefer fixing the
    /// include chain instead.
    ///
    /// Exposed via `--allow-global-arg-cascade` in the CLI (intentionally verbose).
    pub global_arg_cascade: bool,

    /// Apply `default="..."` values declared in `<arg>` elements when the arg is not
    /// provided by the caller.
    ///
    /// **Default: `false` (strict mode).**  When false, a missing arg remains unset and
    /// produces "undefined variable" if referenced, forcing callers to supply every arg
    /// explicitly.  This prevents silent reliance on file-author defaults and discourages
    /// the antipattern of modifying a default value in place of genuine configuration.
    ///
    /// Exposed via `--apply-launch-arg-defaults` in the CLI (intentionally verbose).
    pub apply_arg_defaults: bool,

    /// Resolve launch files from the source workspace (`src/`) rather than from
    /// installed packages on `AMENT_PREFIX_PATH`.
    ///
    /// **Default: `false`.**  Without this flag, every launch file must be present in an
    /// installed package reachable via `AMENT_PREFIX_PATH` (i.e. after `colcon build`).
    /// If a package is missing from the install tree the resolver fails with an explicit
    /// error rather than silently falling back to source.
    ///
    /// When `true`, the source workspace is used for resolution and packages are fetched
    /// on demand via sparse-checkout.  The rendered XML carries a `<!-- PREVIEW -->` header
    /// so consumers can distinguish source-path output from post-build install-path output.
    ///
    /// Exposed via `--preview` in the CLI.
    pub preview: bool,

    /// Allow raw filesystem paths (absolute `/...`) to be passed to `<include file=...>` and
    /// `<param from=...>` in preview mode without producing an error.
    ///
    /// **Default: `false`** — unportable paths are rejected in preview mode.  This flag is
    /// intentionally verbose to discourage hardcoding machine-specific paths.
    ///
    /// Exposed via `--allow-including-unportable-path` in the CLI.
    pub allow_unportable_paths: bool,

    /// Automatically install packages missing from the lockfile via `rosdep`.
    ///
    /// **Default: `false`.**  When a package is required but not found in the lockfile,
    /// the resolver first checks `AMENT_PREFIX_PATH` (always, without this flag).  If it is
    /// still missing and this flag is set, the resolver runs:
    ///   `rosdep install --rosdistro $ROS_DISTRO -y --from-keys <package>`
    /// then retries the AMENT_PREFIX_PATH lookup.
    ///
    /// Requires `ROS_DISTRO` to be set in the environment (sourcing
    /// `/opt/ros/<distro>/setup.bash` sets this automatically).
    ///
    /// Exposed via `--rosdep` in the CLI.
    pub rosdep_fallback: bool,

    /// Allow OpaqueFunction bodies to open files via portable paths
    /// (`$(find-pkg-share pkg)/...`) by resolving them to actual filesystem paths.
    ///
    /// **Default: `false` (strict mode).**  When false, any `open()` or `os.path`
    /// call inside an OpaqueFunction that receives a portable path is recorded as an
    /// error and returns stub data (empty YAML / `False`).  This exposes launch files
    /// that perform file I/O during dependency resolution — an antipattern that makes
    /// static analysis dependent on config file contents.
    ///
    /// When `true`, portable paths inside OpaqueFunction bodies are resolved to actual
    /// filesystem paths using the workspace source index.  Packages that have not been
    /// fetched yet trigger the normal `_PackageNotFetchedError` → fetch → retry loop.
    ///
    /// Use this flag for launch files that call `open(LaunchConfiguration("param_file"))`
    /// where the arg value is a `$(find-pkg-share ...)` path.  The flag name is
    /// intentionally verbose to discourage leaving such patterns in upstream launch files.
    ///
    /// Exposed via `--apply-opaque-file-access` in the CLI.
    pub apply_opaque_file_access: bool,

    /// Expand `<param from="...">` files at resolve time (see [`ResolveOptions::inline_params`]).
    ///
    /// Exposed via `--inline-params` in the CLI.
    pub inline_params: bool,
}

/// Result of recursive launch file resolution
#[derive(Debug, Clone)]
pub struct ResolveResult {
    /// Packages directly required by launch files (via $(find-pkg-share) etc)
    pub direct_packages: HashSet<String>,

    /// All required files by kind
    pub launch_files: Vec<FileDependency>,
    pub param_files: Vec<FileDependency>,
    pub other_files: Vec<FileDependency>,

    /// Packages that were fetched during resolution
    pub fetched_packages: Vec<String>,

    /// Files that were parsed (launch files)
    pub parsed_files: Vec<PathBuf>,

    /// All resolved nodes (accumulated across all parsed launch files)
    pub nodes: Vec<crate::resolver::ResolvedNode>,

    /// Resolved arg context for each included file: (package, share_path) → context
    pub include_args: HashMap<(String, PathBuf), IncludeArgContext>,

    /// Errors encountered during resolution (non-fatal in resolve mode; always fatal in check mode)
    pub errors: Vec<String>,

    /// Warnings encountered during resolution.
    ///
    /// Warnings are non-fatal by default.  In `check --strict` mode they cause a non-zero exit.
    /// Sources: py_resolver shim warnings, unused-arg, excessive-include-arg, and
    /// anti-pattern rules (scoped-false include leakage, deep nesting, etc.).
    pub warnings: Vec<String>,

    /// Informational messages — pedantic warnings suppressed by default.
    ///
    /// Only the count is shown unless `--warn-all` is passed.  Categories:
    ///   - `$(env VAR)` without a fallback (e.g. HOME is always set)
    ///   - unportable param source paths (e.g. ~/autoware_data convention)
    pub infos: Vec<String>,

    /// Per-file declared arg names (from static AST scan), keyed by (package, share_path).
    ///
    /// Populated during resolution by scanning each parsed XML launch file with
    /// [`collect_declared_args`].  Used by the excessive-include-arg check: after a child
    /// file is resolved the caller looks up its declared args and compares them against the
    /// explicit args that were forwarded via `<include><arg .../></include>`.
    pub declared_args_by_file: HashMap<(String, PathBuf), HashSet<String>>,

    /// Globally accumulated SetParameter values discovered during resolution.
    ///
    /// In the real ROS 2 launch system, `SetParameter` writes to a shared mutable
    /// `context.launch_configurations["global_params"]` list that is visible to every
    /// subsequent launch file in the same process.  We replicate this by accumulating
    /// the values here as each Python launch file is resolved; subsequent files receive
    /// the current snapshot via `__global_params__` so their `OpaqueFunction` bodies
    /// can read vehicle dimensions and other cross-file parameters.
    pub global_params: Vec<serde_json::Value>,

    /// The CLI-provided launch arguments passed to the entrypoint (`name:=value` pairs).
    /// Stored here so the renderer can emit them as top-level arg comments when
    /// `--show-args` is active.
    pub initial_args: HashMap<String, String>,
}

impl ResolveResult {
    fn new() -> Self {
        Self {
            direct_packages: HashSet::new(),
            launch_files: Vec::new(),
            param_files: Vec::new(),
            other_files: Vec::new(),
            fetched_packages: Vec::new(),
            parsed_files: Vec::new(),
            nodes: Vec::new(),
            include_args: HashMap::new(),
            errors: Vec::new(),
            warnings: Vec::new(),
            infos: Vec::new(),
            declared_args_by_file: HashMap::new(),
            global_params: Vec::new(),
            initial_args: HashMap::new(),
        }
    }

    fn add_error(&mut self, msg: String) {
        self.errors.push(msg);
    }

    fn add_warning(&mut self, msg: String) {
        self.warnings.push(msg);
    }

    fn add_info(&mut self, msg: String) {
        self.infos.push(msg);
    }
}

/// Resolve a launch file recursively, fetching packages on demand
///
/// This is the main entry point for the `launch-plus resolve` command.
///
/// # Arguments
/// * `lockfile` - Lockfile with package and repository information
/// * `package` - Package name (e.g., "autoware_launch")
/// * `launcher` - Launch file name (e.g., "autoware.launch.xml")
/// * `fetch_dir` - Directory for sparse-checkout (e.g., "src/")
/// * `options` - Fetch options
/// * `initial_args` - CLI-provided launch arguments (`name:=value` pairs)
///
/// # Returns
/// Aggregated result with all packages and files needed
pub fn resolve_launch_recursive(
    lockfile: &Lockfile,
    package: &str,
    launcher: &str,
    fetch_dir: &Path,
    options: &FetchOptions,
    initial_args: HashMap<String, String>,
    workflow_options: &ResolveWorkflowOptions,
) -> crate::Result<ResolveResult> {
    let locator = crate::locator::locator_from_lockfile(fetch_dir, lockfile.clone());

    let mut result = ResolveResult::new();
    result.initial_args = initial_args.clone();
    let mut fetched_packages: HashSet<String> = HashSet::new();

    // Warn early if ROS_DISTRO is not set — many fallbacks depend on it.
    if std::env::var("ROS_DISTRO")
        .map(|v| v.is_empty())
        .unwrap_or(true)
    {
        result.add_warning(
            "ROS_DISTRO is not set; source /opt/ros/<distro>/setup.bash for full functionality. \
             Packages not in the lockfile will not be found via AMENT_PREFIX_PATH."
                .to_string(),
        );
    }

    // Start with the entrypoint; CLI args apply only to this top-level file
    let share_path = PathBuf::from("launch").join(launcher);
    resolve_file_recursive(
        lockfile,
        &locator,
        package,
        &share_path,
        fetch_dir,
        options,
        initial_args,
        workflow_options,
        &HashMap::new(), // persisted_arg_context: empty at root
        &mut result,
        &mut fetched_packages,
        vec![], // parent_chain: empty Vec<(String, PathBuf)> for root
    );

    info!(
        "Resolution complete: {} direct packages, {} packages fetched, {} errors, {} warnings",
        result.direct_packages.len(),
        result.fetched_packages.len(),
        result.errors.len(),
        result.warnings.len()
    );

    Ok(result)
}

/// Check if a file is an XML launch file that we can parse
fn is_xml_launch_file(path: &Path) -> bool {
    let path_str = path.to_string_lossy();
    path_str.ends_with(".launch.xml") || path_str.ends_with(".xml")
}

/// Check if a file is a Python launch file
fn is_python_launch_file(path: &Path) -> bool {
    path.to_string_lossy().ends_with(".launch.py")
}

/// The py_resolver.py script embedded in the binary so it can be extracted to a temp file.
const PY_RESOLVER_SCRIPT: &str = include_str!("../../../python/launch_plus/py_resolver.py");

/// Run the Python launch resolver on a .launch.py file.
///
/// Returns a JSON string with packages, includes, nodes, and warnings.
fn run_py_resolver(
    file_path: &Path,
    initial_args: &HashMap<String, String>,
    package_shares: &HashMap<String, String>,
    lockfile_package_names: &[String],
    persisted_global_params: &[serde_json::Value],
    workflow_options: &ResolveWorkflowOptions,
) -> crate::Result<PyResolverOutput> {
    use std::io::Write;
    use std::process::{Command, Stdio};

    // Write the shim script to a temp file (always overwrite so updates take effect)
    let tmp_dir = std::env::temp_dir().join("launch-plus");
    std::fs::create_dir_all(&tmp_dir)
        .map_err(|e| crate::Error::Git(format!("failed to create tmp dir: {e}")))?;
    let script_path = tmp_dir.join("py_resolver.py");
    {
        let mut f = std::fs::File::create(&script_path)
            .map_err(|e| crate::Error::Git(format!("failed to write py_resolver.py: {e}")))?;
        f.write_all(PY_RESOLVER_SCRIPT.as_bytes())
            .map_err(|e| crate::Error::Git(format!("failed to write py_resolver.py: {e}")))?;
    }

    // Inject persisted global_params into args under the reserved key __global_params__.
    // py_resolver strips this key before building the launch context and pre-populates
    // context.launch_configurations["global_params"] from it.
    let mut args_with_globals = initial_args.clone();
    if !persisted_global_params.is_empty() {
        let gp_json = serde_json::to_string(persisted_global_params)
            .map_err(|e| crate::Error::Git(format!("failed to serialize global_params: {e}")))?;
        args_with_globals.insert("__global_params__".to_string(), gp_json);
    }

    let args_json = serde_json::to_string(&args_with_globals)
        .map_err(|e| crate::Error::Git(format!("failed to serialize launch args: {e}")))?;
    let shares_json = serde_json::to_string(package_shares)
        .map_err(|e| crate::Error::Git(format!("failed to serialize package_shares: {e}")))?;

    // Pass workflow options as a JSON flags object (sys.argv[4] in py_resolver).
    let flags = serde_json::json!({
        "apply_opaque_file_access": workflow_options.apply_opaque_file_access,
        "preview": workflow_options.preview,
        // Full set of lockfile package names so py_resolver can enforce that lockfile
        // packages always resolve from source and never fall through to AMENT_PREFIX_PATH.
        "lockfile_packages": lockfile_package_names,
    });
    let flags_json = flags.to_string();

    let script_str = script_path.to_str().ok_or_else(|| {
        crate::Error::Git("py_resolver script path is not valid UTF-8".to_string())
    })?;
    let file_str = file_path.to_str().ok_or_else(|| {
        crate::Error::Git(format!(
            "launch file path is not valid UTF-8: {}",
            file_path.display()
        ))
    })?;
    let output = Command::new("python3")
        .args([script_str, file_str, &args_json, &shares_json, &flags_json])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| crate::Error::Git(format!("failed to run python3: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::Git(format!(
            "py_resolver failed for {}: {}",
            file_path.display(),
            stderr.trim()
        )));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str::<PyResolverOutput>(&stdout).map_err(|e| {
        crate::Error::Git(format!(
            "failed to parse py_resolver output for {}: {e}",
            file_path.display()
        ))
    })
}

#[derive(Debug, serde::Deserialize)]
struct PyResolverOutput {
    packages: Vec<String>,
    /// Raw include path strings (kept for serde compatibility; use `include_deps` instead).
    #[allow(dead_code)]
    includes: Vec<String>,
    nodes: Vec<PyResolvedNode>,
    warnings: Vec<String>,
    /// Arg names and their resolved defaults declared by DeclareLaunchArgument.
    /// Used by the orchestrator to forward defaults to child files when
    /// `apply_arg_defaults` is enabled, mirroring XML `<arg default="...">` behaviour.
    #[serde(default)]
    declared_args: Vec<PyDeclaredArg>,
    /// Accumulated SetParameter calls from this file: [[name, value], ...].
    /// In the real ROS 2 launch system, SetParameter.execute() appends (name, value)
    /// tuples to context.launch_configurations["global_params"].  The orchestrator
    /// persists this list and passes it to subsequent Python file invocations so they
    /// can read the global parameter context established by earlier sibling files.
    #[serde(default)]
    global_params: Vec<serde_json::Value>,
    /// launch_arguments captured from IncludeLaunchDescription calls in this file:
    /// {include_path: {arg_name: value}}.  The orchestrator uses these to forward
    /// args to the included file's py_resolver invocation, mirroring ROS 2 behaviour
    /// where IncludeLaunchDescription scopes the provided launch_arguments into the
    /// child file's launch context.
    #[serde(default)]
    include_args: HashMap<String, HashMap<String, String>>,
    /// Raw param file path strings (kept for serde compatibility; use `param_file_deps` instead).
    #[serde(default)]
    #[allow(dead_code)]
    param_files: Vec<String>,
    /// Errors encountered during Python file resolution (non-recoverable).
    /// Promoted to result.add_error() by the caller.
    #[serde(default)]
    errors: Vec<String>,
    /// Lockfile packages whose source directories were not found on disk.
    /// Non-empty when get_package_share_directory() was called for a lockfile package
    /// that hasn't been cloned yet.  The caller fetches these and retries run_py_resolver.
    #[serde(default)]
    packages_to_fetch: Vec<String>,
    /// SetLaunchConfiguration calls: {name: value}.
    /// Propagated into the calling XML file's substitution context so that
    /// subsequent $(var name) references resolve correctly.
    #[serde(default)]
    set_launch_configurations: HashMap<String, String>,
    /// Structured include dependencies: [{package, share_path}].
    /// Extracted by the Python resolver from portable or AMENT install paths,
    /// eliminating the need for reverse path mapping on the Rust side.
    #[serde(default)]
    include_deps: Vec<PyFileDep>,
    /// Structured param file dependencies: [{package, share_path}].
    #[serde(default)]
    param_file_deps: Vec<PyFileDep>,
}

#[derive(Debug, serde::Deserialize, Clone, PartialEq)]
struct PyFileDep {
    package: String,
    share_path: String,
    /// Original path string (for looking up include_args keyed by path).
    #[serde(default)]
    path: String,
}

#[derive(Debug, serde::Deserialize, Default, PartialEq)]
#[serde(rename_all = "snake_case")]
enum PyNodeKind {
    #[default]
    Node,
    Container,
    LoadComposable,
    SetParameter,
    Executable,
}

#[derive(Debug, serde::Deserialize)]
struct PyComposablePlugin {
    package: String,
    plugin: String,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    parameters: BTreeMap<String, String>,
    #[serde(default)]
    remappings: Vec<(String, String)>,
}

#[derive(Debug, serde::Deserialize)]
struct PyResolvedNode {
    package: String,
    executable: String,
    name: String,
    /// PushRosNamespace stack at node-resolution time; combined with
    /// `explicit_namespace` by `py_output_to_parsed` via `effective_namespace`.
    #[serde(default)]
    namespace_stack: Vec<String>,
    /// The node's own `namespace=` kwarg, resolved to a string by Python.
    /// May be absolute (starts with `/`) or relative.
    #[serde(default)]
    explicit_namespace: Option<String>,
    #[serde(default)]
    parameters: BTreeMap<String, String>,
    #[serde(default)]
    remappings: Vec<(String, String)>,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default)]
    kind: PyNodeKind,
    #[serde(default)]
    plugins: Vec<PyComposablePlugin>,
    #[serde(default)]
    target: Option<String>,
    /// For `kind == SetParameter`: the resolved parameter value string.
    #[serde(default)]
    param_value: Option<String>,
    /// For `kind == Executable`: the resolved command string.
    #[serde(default)]
    cmd: Option<String>,
    /// For `kind == Executable`: whether to run via a shell.
    #[serde(default)]
    shell: bool,
}

#[derive(Debug, serde::Deserialize)]
struct PyDeclaredArg {
    name: String,
    default: String,
}

/// Fetch a package if not already fetched
///
/// Returns true if the package was fetched or already exists, false on error.
fn ensure_package_fetched(
    lockfile: &Lockfile,
    package: &str,
    fetch_dir: &Path,
    options: &FetchOptions,
    result: &mut ResolveResult,
    fetched_packages: &mut HashSet<String>,
) -> bool {
    // Already fetched in this session
    if fetched_packages.contains(package) {
        return true;
    }

    // Check if package exists in lockfile
    let Some(pkg_lock) = lockfile.packages.get(package) else {
        result.add_error(format!("package '{}' not found in lockfile", package));
        return false;
    };

    // Check if already on disk
    let pkg_path = fetch_dir.join(&pkg_lock.repo).join(&pkg_lock.path);
    if pkg_path.exists() && pkg_path.join("package.xml").exists() {
        debug!(
            "Package {} already fetched at {}",
            package,
            pkg_path.display()
        );
        fetched_packages.insert(package.to_string());
        return true;
    }

    // Fetch the package
    info!("Fetching package: {}", package);
    match fetch_packages(&[package.to_string()], lockfile, fetch_dir, options) {
        Ok(_) => {
            result.fetched_packages.push(package.to_string());
            fetched_packages.insert(package.to_string());
            true
        }
        Err(e) => {
            result.add_error(format!("failed to fetch package '{}': {}", package, e));
            false
        }
    }
}

/// Attempt to install a missing ROS package via `rosdep`.
///
/// Delegates to [`crate::rosdep::rosdep_install`] and translates the result
/// into a boolean + error on `result`.
fn try_rosdep_install(package: &str, result: &mut ResolveResult) -> bool {
    match crate::rosdep::rosdep_install(&[package]) {
        Ok(()) => true,
        Err(e) => {
            result.add_error(format!("{e}"));
            false
        }
    }
}

/// Convert a [`ResolvedLaunch`] (XML/YAML output) into the unified [`ParsedLaunchFile`] IR.
///
/// Maps `required_files` into `launch_includes`, `param_files`, and `other_files`,
/// extracting explicit include args from `include_args`.
///
/// Any `required_abs_files` that couldn't be parsed by `extract_file_dependency` are
/// reported as errors — this indicates a non-standard path pattern (anti-pattern in
/// ROS 2 launch files that don't use `$(find-pkg-share ...)`).
fn resolved_launch_to_parsed(resolved: ResolvedLaunch) -> ParsedLaunchFile {
    let mut launch_includes = Vec::new();
    let mut param_files = Vec::new();
    let mut other_files = Vec::new();
    let mut errors = resolved.errors.clone();

    for dep in &resolved.required_files {
        match dep.kind {
            DependencyKind::Launch => {
                let explicit_args = resolved
                    .include_args
                    .get(&(dep.package.clone(), dep.share_path.clone()))
                    .map(|ctx| ctx.explicit.clone())
                    .unwrap_or_default();
                launch_includes.push(LaunchInclude {
                    package: dep.package.clone(),
                    share_path: dep.share_path.clone(),
                    explicit_args,
                });
            }
            DependencyKind::Param => param_files.push(dep.clone()),
            DependencyKind::Other => other_files.push(dep.clone()),
        }
    }

    for (abs_path, kind, _ctx) in &resolved.required_abs_files {
        errors.push(format!(
            "path '{}' ({:?}) could not be decomposed into (package, share_path) — \
             launch files should use $(find-pkg-share <pkg>) instead of absolute paths",
            abs_path.display(),
            kind,
        ));
    }

    ParsedLaunchFile {
        packages: resolved.required_packages.into_iter().collect(),
        nodes: resolved.nodes,
        launch_includes,
        param_files,
        other_files,
        declared_arg_defaults: resolved.declared_arg_defaults,
        global_params: Vec::new(), // XML/YAML has no SetParameter
        warnings: resolved.warnings,
        errors,
        infos: resolved.infos,
    }
}

/// Convert a [`PyResolverOutput`] (Python shim output) into the unified [`ParsedLaunchFile`] IR.
///
/// Uses structured `include_deps` / `param_file_deps` emitted by the Python resolver
/// (already decomposed into `(package, share_path)` pairs) — no reverse path mapping needed.
/// Warnings are intentionally excluded here — the caller handles them with py_resolver-specific
/// formatting (package://path: prefix) before calling this function.
fn py_output_to_parsed(py_output: PyResolverOutput) -> ParsedLaunchFile {
    let nodes = py_output
        .nodes
        .iter()
        .map(|n| {
            let kind = match n.kind {
                PyNodeKind::Container => NodeKind::Container {
                    plugins: n
                        .plugins
                        .iter()
                        .map(|p| ComposablePlugin {
                            package: p.package.clone(),
                            plugin: p.plugin.clone(),
                            name: p.name.clone(),
                            parameters: p.parameters.clone(),
                            remappings: p.remappings.clone(),
                            param_files: vec![],
                        })
                        .collect(),
                },
                PyNodeKind::LoadComposable => NodeKind::LoadComposable {
                    target: n.target.clone().unwrap_or_default(),
                    plugins: n
                        .plugins
                        .iter()
                        .map(|p| ComposablePlugin {
                            package: p.package.clone(),
                            plugin: p.plugin.clone(),
                            name: p.name.clone(),
                            parameters: p.parameters.clone(),
                            remappings: p.remappings.clone(),
                            param_files: vec![],
                        })
                        .collect(),
                },
                PyNodeKind::Node => NodeKind::Node,
                PyNodeKind::SetParameter => NodeKind::SetParameter {
                    name: n.name.clone(),
                    value: n.param_value.clone().unwrap_or_default(),
                },
                PyNodeKind::Executable => NodeKind::Executable {
                    cmd: n.cmd.clone().unwrap_or_default(),
                    name: if n.name.is_empty() {
                        None
                    } else {
                        Some(n.name.clone())
                    },
                    shell: n.shell,
                },
            };
            ResolvedNode {
                package: n.package.clone(),
                executable: n.executable.clone(),
                name: if n.name.is_empty() {
                    None
                } else {
                    Some(n.name.clone())
                },
                namespace: crate::resolver::effective_namespace(
                    &n.namespace_stack,
                    n.explicit_namespace.as_deref(),
                ),
                namespace_stack: n.namespace_stack.clone(),
                parameters: n.parameters.clone(),
                remappings: n.remappings.clone(),
                env: n.env.clone(),
                source: None,              // set by process_parsed_file
                include_chain: Vec::new(), // set by process_parsed_file
                kind,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None, // Python launch API has no respawn support yet
                respawn_delay: None,
            }
        })
        .collect();

    let launch_includes = py_output
        .include_deps
        .iter()
        .map(|dep| {
            let explicit_args = py_output
                .include_args
                .get(&dep.path)
                .cloned()
                .unwrap_or_default();
            LaunchInclude {
                package: dep.package.clone(),
                share_path: PathBuf::from(&dep.share_path),
                explicit_args,
            }
        })
        .collect();

    let declared_arg_defaults: HashMap<String, String> = py_output
        .declared_args
        .iter()
        .map(|a| (a.name.clone(), a.default.clone()))
        .collect();

    let param_files = py_output
        .param_file_deps
        .iter()
        .map(|dep| FileDependency {
            package: dep.package.clone(),
            share_path: PathBuf::from(&dep.share_path),
            kind: DependencyKind::Param,
        })
        .collect();

    ParsedLaunchFile {
        packages: py_output.packages,
        nodes,
        launch_includes,
        param_files,
        other_files: Vec::new(),
        declared_arg_defaults,
        global_params: py_output.global_params,
        warnings: Vec::new(), // warnings emitted separately by the caller with py_resolver formatting
        errors: Vec::new(),
        infos: Vec::new(),
    }
}

/// Common post-processing for any parsed launch file (XML, YAML, or Python).
///
/// Accumulates packages/files/nodes into `result`, updates declared-arg tracking,
/// and recurses into each launch include.  Both `resolve_file_recursive` (XML/YAML)
/// and `resolve_python_file_recursive` (Python) call this after producing a
/// [`ParsedLaunchFile`].
#[allow(clippy::too_many_arguments)]
fn process_parsed_file(
    parsed: ParsedLaunchFile,
    package: &str,
    share_path: &Path,
    file_path: &Path,
    current_chain: &[(String, PathBuf)],
    effective_args: &HashMap<String, String>,
    persisted_arg_context: &HashMap<String, String>,
    result: &mut ResolveResult,
    lockfile: &Lockfile,
    locator: &PackageLocator,
    fetch_dir: &Path,
    fetched_packages: &mut HashSet<String>,
    workflow_options: &ResolveWorkflowOptions,
    options: &FetchOptions,
) {
    // Accumulate direct packages
    result
        .direct_packages
        .extend(parsed.packages.iter().cloned());

    // Dedup-extend param_files and other_files
    for dep in parsed.param_files {
        if !result
            .param_files
            .iter()
            .any(|f| f.package == dep.package && f.share_path == dep.share_path)
        {
            result.param_files.push(dep);
        }
    }
    for dep in parsed.other_files {
        if !result
            .other_files
            .iter()
            .any(|f| f.package == dep.package && f.share_path == dep.share_path)
        {
            result.other_files.push(dep);
        }
    }

    result.warnings.extend(parsed.warnings);
    result.errors.extend(parsed.errors);
    result.infos.extend(parsed.infos);
    // Extend global_params before recursion so subsequent sibling files see them.
    result.global_params.extend(parsed.global_params);

    // Record parsed file
    result.parsed_files.push(file_path.to_path_buf());

    // Set include_chain and source on all nodes, then accumulate.
    // IncludeMarker nodes (from inline resolver) get a chain that includes their own source.
    let source = (package.to_string(), share_path.to_path_buf());
    for mut node in parsed.nodes {
        if matches!(node.kind, NodeKind::IncludeMarker) {
            let mut c = current_chain.to_vec();
            if let Some(ref src) = node.source {
                c.push(src.clone());
            }
            node.include_chain = c;
        } else {
            node.include_chain = current_chain.to_vec();
            node.source.get_or_insert(source.clone());
        }
        result.nodes.push(node);
    }

    // Store declared args so the excessive-include-arg check can run after recursing.
    result.declared_args_by_file.insert(
        (package.to_string(), share_path.to_path_buf()),
        parsed.declared_arg_defaults.keys().cloned().collect(),
    );

    // Build next_persisted: current persisted context + this file's declared defaults.
    let next_persisted: HashMap<String, String> = persisted_arg_context
        .iter()
        .chain(parsed.declared_arg_defaults.iter())
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();

    // Recurse into launch includes
    for include in parsed.launch_includes {
        // Add to launch files (dedup)
        if !result
            .launch_files
            .iter()
            .any(|f| f.package == include.package && f.share_path == include.share_path)
        {
            result.launch_files.push(FileDependency {
                package: include.package.clone(),
                share_path: include.share_path.clone(),
                kind: DependencyKind::Launch,
            });
        }

        // Build child args:
        // - cascade mode: parent effective_args as base, explicit args override
        // - strict mode: only explicit args
        // Persisted declared defaults from ancestors fill in remaining gaps at lowest priority.
        let mut child_args = if workflow_options.global_arg_cascade {
            let mut m = effective_args.clone();
            for (k, v) in &include.explicit_args {
                m.insert(k.clone(), v.clone()); // explicit overrides cascade
            }
            m
        } else {
            include.explicit_args.clone()
        };
        for (k, v) in &next_persisted {
            child_args.entry(k.clone()).or_insert_with(|| v.clone());
        }

        let nodes_before = result.nodes.len();
        resolve_file_recursive(
            lockfile,
            locator,
            &include.package,
            &include.share_path,
            fetch_dir,
            options,
            child_args,
            workflow_options,
            &next_persisted,
            result,
            fetched_packages,
            current_chain.to_vec(),
        );

        // Inject IncludeMarker if the child file (and all descendants) produced no nodes.
        if result.nodes.len() == nodes_before {
            let mut chain = current_chain.to_vec();
            chain.push((include.package.clone(), include.share_path.clone()));
            result.nodes.push(ResolvedNode {
                source: Some((include.package.clone(), include.share_path.clone())),
                include_chain: chain,
                kind: NodeKind::IncludeMarker,
                ..Default::default()
            });
        }

        // Excessive include arg check: warn about args forwarded to a child that the child
        // never declares.  Skip in global-cascade mode where children may use args via
        // LaunchConfiguration without declaring them.
        if !workflow_options.global_arg_cascade && !include.explicit_args.is_empty() {
            let child_declared = result
                .declared_args_by_file
                .get(&(include.package.clone(), include.share_path.clone()))
                .cloned()
                .unwrap_or_default();
            let explicit_names: HashSet<String> = include.explicit_args.keys().cloned().collect();
            let mut excessive: Vec<String> = explicit_names
                .difference(&child_declared)
                .cloned()
                .collect();
            excessive.sort();
            for arg_name in excessive {
                result.add_warning(format!(
                    "{}://{}: excessive include arg '{}' — {}://{} does not declare <arg name=\"{}\"/>",
                    package,
                    share_path.display(),
                    arg_name,
                    include.package,
                    include.share_path.display(),
                    arg_name
                ));
            }
        }
    }
}

/// Resolve a Python launch file recursively
///
/// Calls the embedded py_resolver.py shim, collects packages/nodes/includes,
/// and recurses into any discovered launch file includes.
fn resolve_python_file_recursive(
    lockfile: &Lockfile,
    locator: &PackageLocator,
    package: &str,
    share_path: &Path,
    fetch_dir: &Path,
    options: &FetchOptions,
    initial_args: &HashMap<String, String>,
    workflow_options: &ResolveWorkflowOptions,
    persisted_arg_context: &HashMap<String, String>,
    result: &mut ResolveResult,
    fetched_packages: &mut HashSet<String>,
    parent_chain: Vec<(String, PathBuf)>,
) {
    // Resolve the file path based on mode (same logic as XML resolver).
    let file_path = if workflow_options.preview {
        if lockfile.packages.contains_key(package) {
            if !ensure_package_fetched(
                lockfile,
                package,
                fetch_dir,
                options,
                result,
                fetched_packages,
            ) {
                return;
            }
            match locator.resolve_share_file(package, share_path) {
                Some(p) => p,
                None => {
                    result.add_error(format!(
                        "could not locate Python launch file '{}' in package '{}'",
                        share_path.display(),
                        package
                    ));
                    return;
                }
            }
        } else {
            // Not in lockfile: try AMENT_PREFIX_PATH (ROS buildfarm or manually installed).
            match locator.resolve_install_file(package, share_path) {
                Some(p) => p,
                None => {
                    if workflow_options.rosdep_fallback {
                        if try_rosdep_install(package, result) {
                            match locator.resolve_install_file(package, share_path) {
                                Some(p) => p,
                                None => {
                                    result.add_error(format!(
                                        "package '{}' not found even after rosdep install",
                                        package
                                    ));
                                    return;
                                }
                            }
                        } else {
                            return;
                        }
                    } else {
                        result.add_error(format!(
                            "package '{}' not found in lockfile or AMENT_PREFIX_PATH; \
                             use --rosdep to install missing packages automatically",
                            package
                        ));
                        return;
                    }
                }
            }
        }
    } else {
        match locator.resolve_install_file(package, share_path) {
            Some(p) => p,
            None => {
                result.add_error(format!(
                    "{}://{} not found in AMENT_PREFIX_PATH; \
                     run 'colcon build' first, or use --preview to resolve from source workspace",
                    package,
                    share_path.display()
                ));
                return;
            }
        }
    };

    // Collect package share paths for py_resolver.
    // Preview mode: workspace source paths (lockfile + AMENT fallback).
    // Non-preview mode: AMENT_PREFIX_PATH only (installed artifacts).
    let package_shares = if workflow_options.preview {
        locator.all_package_shares()
    } else {
        locator.all_install_shares()
    };

    // Full list of lockfile package names — passed to py_resolver so it can enforce
    // that lockfile packages always resolve from source and never fall through to
    // AMENT_PREFIX_PATH (see _resolve_pkg_share in py_resolver.py).
    let lockfile_pkg_list: Vec<String> = lockfile.packages.keys().cloned().collect();

    // Snapshot the globally accumulated SetParameter values accumulated so far.
    // This file may depend on SetParameter calls from sibling/ancestor files (e.g.
    // vehicle_info.launch.py setting front_overhang before ground_segmentation.launch.py
    // reads it).  The snapshot is passed to py_resolver via __global_params__ so that
    // OpaqueFunction bodies can read cross-file parameters.
    let current_global_params = result.global_params.clone();

    // Run the Python resolver shim, retrying if lockfile packages are missing from disk.
    // py_resolver signals missing packages via `packages_to_fetch` in its JSON output
    // (raised by get_package_share_directory() when a source directory doesn't exist).
    // We fetch the missing packages and retry; bounded to avoid infinite loops.
    let py_output = {
        const MAX_FETCH_RETRIES: usize = 3;
        let mut retries_remaining = MAX_FETCH_RETRIES;
        loop {
            let out = match run_py_resolver(
                &file_path,
                initial_args,
                &package_shares,
                &lockfile_pkg_list,
                &current_global_params,
                workflow_options,
            ) {
                Ok(out) => out,
                Err(e) => {
                    result.add_error(format!(
                        "failed to resolve Python launch file {}: {}",
                        file_path.display(),
                        e
                    ));
                    return;
                }
            };

            if out.packages_to_fetch.is_empty() || retries_remaining == 0 {
                break out;
            }

            // Fetch every missing package, then retry py_resolver so that
            // get_package_share_directory() finds them on disk this time.
            let mut any_fetched = false;
            for pkg in &out.packages_to_fetch {
                if ensure_package_fetched(
                    lockfile,
                    pkg,
                    fetch_dir,
                    options,
                    result,
                    fetched_packages,
                ) {
                    any_fetched = true;
                }
            }
            if !any_fetched {
                // Nothing could be fetched (e.g. all packages missing from lockfile).
                // Stop retrying — output may be incomplete.
                let missing: Vec<_> = out
                    .packages_to_fetch
                    .iter()
                    .filter(|p| !fetched_packages.contains(p.as_str()))
                    .cloned()
                    .collect();
                if !missing.is_empty() {
                    result.add_warning(format!(
                        "Python resolver requested packages not in lockfile: {}; \
                         resolution may be incomplete",
                        missing.join(", ")
                    ));
                }
                break out;
            }
            retries_remaining -= 1;
        }
    };

    // Build include chain for this file as (package, share_path) pairs.
    let mut current_chain = parent_chain;
    current_chain.push((package.to_string(), share_path.to_path_buf()));

    // Build effective args before consuming py_output: merge initial_args with declared
    // defaults (initial_args wins).  Passed to process_parsed_file for cascade-mode children.
    let mut effective_args = initial_args.clone();
    if workflow_options.apply_arg_defaults {
        for declared in &py_output.declared_args {
            effective_args
                .entry(declared.name.clone())
                .or_insert_with(|| declared.default.clone());
        }
    }

    // Promote shim warnings with py_resolver-specific formatting (package://path: prefix).
    // Done before consuming py_output; py_output_to_parsed intentionally omits warnings.
    for warning in &py_output.warnings {
        warn!("py_resolver [{}]: {}", file_path.display(), warning);
        result.add_warning(format!(
            "{}://{}: {}",
            package,
            share_path.display(),
            warning
        ));
    }
    // Promote shim errors (non-recoverable Python exceptions).
    for error in &py_output.errors {
        warn!("py_resolver error [{}]: {}", file_path.display(), error);
        result.add_error(format!("{}://{}: {}", package, share_path.display(), error));
    }

    // Convert py_output to the unified IR and delegate all common post-processing.
    let parsed = py_output_to_parsed(py_output);
    process_parsed_file(
        parsed,
        package,
        share_path,
        &file_path,
        &current_chain,
        &effective_args,
        persisted_arg_context,
        result,
        lockfile,
        locator,
        fetch_dir,
        fetched_packages,
        workflow_options,
        options,
    );
}

/// Internal recursive resolver
///
/// This function is resilient - it catches errors and records them
/// while continuing to resolve as much as possible.
fn resolve_file_recursive(
    lockfile: &Lockfile,
    locator: &PackageLocator,
    package: &str,
    share_path: &Path,
    fetch_dir: &Path,
    options: &FetchOptions,
    initial_args: HashMap<String, String>,
    workflow_options: &ResolveWorkflowOptions,
    persisted_arg_context: &HashMap<String, String>,
    result: &mut ResolveResult,
    fetched_packages: &mut HashSet<String>,
    parent_chain: Vec<(String, PathBuf)>,
) {
    // Cycle detection: if this exact file already appears anywhere in the current
    // include chain (parent → grandparent → ...) we are in a recursive include loop
    // — stop immediately.  This is the only guard needed; explicit deduplication of
    // same-file same-args invocations is intentionally absent because ROS 2's launch
    // system treats every <include> as an independent instantiation, and two includes
    // of the same file (even with identical args) may produce distinct nodes when
    // wrapped in different PushRosNamespace / <group namespace="..."> contexts.
    if parent_chain
        .iter()
        .any(|(p, s)| p == package && s == share_path)
    {
        debug!(
            "Cycle detected for {}:{}, stopping recursion",
            package,
            share_path.display()
        );
        return;
    }

    debug!(
        "Resolving launch file: {}:{}",
        package,
        share_path.display()
    );

    // Route to appropriate handler based on file type
    if is_python_launch_file(share_path) {
        resolve_python_file_recursive(
            lockfile,
            locator,
            package,
            share_path,
            fetch_dir,
            options,
            &initial_args,
            workflow_options,
            persisted_arg_context,
            result,
            fetched_packages,
            parent_chain,
        );
        return;
    }

    if !is_xml_launch_file(share_path) {
        debug!(
            "Skipping unsupported launch file type: {}:{}",
            package,
            share_path.display()
        );
        return;
    }

    // Resolve the file path based on mode:
    //   preview: fetch from source workspace (lockfile) with AMENT_PREFIX_PATH fallback
    //   non-preview: look up from AMENT_PREFIX_PATH (must be installed after colcon build)
    let file_path = if workflow_options.preview {
        if lockfile.packages.contains_key(package) {
            // Package is in lockfile: fetch workspace source and resolve from there.
            if !ensure_package_fetched(
                lockfile,
                package,
                fetch_dir,
                options,
                result,
                fetched_packages,
            ) {
                return;
            }
            match locator.resolve_share_file(package, share_path) {
                Some(p) => p,
                None => {
                    result.add_error(format!(
                        "package '{}' is in lockfile but the file {}://{} was not found in the workspace source",
                        package, package, share_path.display()
                    ));
                    return;
                }
            }
        } else {
            // Package is NOT in lockfile (e.g. a ROS buildfarm package like rosbridge_server).
            // Fall back to AMENT_PREFIX_PATH (requires the package to be installed via apt/rosdep).
            match locator.resolve_install_file(package, share_path) {
                Some(p) => p,
                None => {
                    if workflow_options.rosdep_fallback {
                        // Try to install via rosdep, then retry the lookup.
                        if try_rosdep_install(package, result) {
                            match locator.resolve_install_file(package, share_path) {
                                Some(p) => p,
                                None => {
                                    result.add_error(format!(
                                        "package '{}' not found even after rosdep install; \
                                         it may need to be added to the lockfile",
                                        package
                                    ));
                                    return;
                                }
                            }
                        } else {
                            return; // error already added by try_rosdep_install
                        }
                    } else {
                        result.add_error(format!(
                            "package '{}' not found in lockfile or AMENT_PREFIX_PATH; \
                             if it is a ROS buildfarm package, install it with \
                             `rosdep install -y --from-keys {}` or use --rosdep to \
                             install missing packages automatically",
                            package, package
                        ));
                        return;
                    }
                }
            }
        }
    } else {
        match locator.resolve_install_file(package, share_path) {
            Some(p) => p,
            None => {
                result.add_error(format!(
                    "{}://{} not found in AMENT_PREFIX_PATH; \
                     run 'colcon build' first, or use --preview to resolve from source workspace",
                    package,
                    share_path.display()
                ));
                return;
            }
        }
    };

    // Parse the launch file
    let content = match std::fs::read_to_string(&file_path) {
        Ok(c) => c,
        Err(e) => {
            result.add_error(format!("failed to read {}: {}", file_path.display(), e));
            return;
        }
    };

    let ast = match parse_launch_xml(&content, &file_path) {
        Ok(a) => a,
        Err(e) => {
            result.add_error(format!("failed to parse {}: {}", file_path.display(), e));
            return;
        }
    };

    // Static AST scan for declared/referenced arg names (must happen before initial_args is moved).
    let declared_arg_names = collect_declared_args(&ast.elements);
    let referenced_arg_names = collect_arg_and_var_refs(&ast.elements);

    // Anti-pattern static scans.
    let scoped_false_includes = collect_scoped_false_includes(&ast.elements);
    let env_no_fallback = collect_env_without_fallback(&ast.elements);

    // Resolve (without following includes - we handle that here).
    // Provide a pkg_share_resolver so that $(find-pkg-share X) substitutions are
    // expanded to real paths — necessary for inline-resolved includes (e.g. YAML files)
    // whose content is loaded from the resolved path at resolution time.
    let locator_for_ctx = locator.clone();
    let locator_for_cb = locator.clone();
    let package_shares_for_cb = if workflow_options.preview {
        locator.all_package_shares()
    } else {
        locator.all_install_shares()
    };
    let workflow_options_for_cb = workflow_options.clone();
    let is_preview = workflow_options.preview;
    let lockfile_pkg_names: Arc<HashSet<String>> =
        Arc::new(lockfile.packages.keys().cloned().collect());
    let mut ctx = SubstitutionContext {
        launch_file_dir: Some(file_path.parent().unwrap_or(Path::new("/")).to_path_buf()),
        launch_file_path: Some(file_path.clone()),
        pkg_share_resolver: Some(Arc::new(move |pkg: &str| {
            if is_preview {
                locator_for_ctx.locate_package_share(pkg)
            } else {
                locator_for_ctx.locate_install_share(pkg)
            }
        })),
        preview_mode: workflow_options.preview,
        lockfile_packages: lockfile_pkg_names,
        rosdep_fallback: workflow_options.rosdep_fallback,
        ..Default::default()
    };
    // Build a callback that runs py_resolver inline on Python includes so that
    // SetLaunchConfiguration side-effects (e.g. current_ros_namespace) become visible
    // to subsequent $(var ...) substitutions in the same parent XML file.
    // Note: inline calls receive empty global_params (not the accumulated SetParameter
    // state) because the inline callback only extracts SetLaunchConfiguration side-effects,
    // not full resolution output.  Full resolution (with global_params) happens later in
    // resolve_python_file_recursive when the orchestrator processes the include.
    let lockfile_pkg_list_for_cb: Vec<String> = lockfile.packages.keys().cloned().collect();
    let python_cfg_callback: Arc<
        dyn Fn(&Path, &HashMap<String, String>) -> HashMap<String, String> + Send + Sync,
    > = Arc::new(move |py_path: &Path, args: &HashMap<String, String>| {
        // Augment args with the package shares from AMENT_PREFIX_PATH so that
        // Python files that call get_package_share_directory() resolve correctly.
        let _ = &locator_for_cb; // keep alive
        match run_py_resolver(
            py_path,
            args,
            &package_shares_for_cb,
            &lockfile_pkg_list_for_cb,
            &[],
            &workflow_options_for_cb,
        ) {
            Ok(output) => output.set_launch_configurations,
            Err(e) => {
                tracing::warn!(
                    "inline py_resolver for '{}' failed: {e} — \
                     SetLaunchConfiguration side-effects from this file will be missing",
                    py_path.display()
                );
                HashMap::new()
            }
        }
    });
    let resolve_options = ResolveOptions {
        apply_arg_defaults: workflow_options.apply_arg_defaults,
        allow_unportable_paths: workflow_options.allow_unportable_paths,
        inline_params: workflow_options.inline_params,
        python_cfg_callback: Some(python_cfg_callback),
        ..Default::default()
    };

    // Clone initial_args as effective_args before resolve_launch consumes it.
    // Used by process_parsed_file to build cascade-mode child args.
    let effective_args = initial_args.clone();

    let resolved = match resolve_launch(&ast, initial_args, &mut ctx, &resolve_options) {
        Ok(r) => r,
        Err(e) => {
            result.add_error(format!("failed to resolve {}: {}", file_path.display(), e));
            return;
        }
    };

    // Build include chain: parent_chain + (package, share_path).
    let mut current_chain = parent_chain;
    current_chain.push((package.to_string(), share_path.to_path_buf()));

    // --- XML-specific anti-pattern checks ---

    // Unused arg: warn about args declared in this file but never referenced via $(arg)
    // or $(var).  Skipped in global-cascade mode where args flow implicitly.
    if !workflow_options.global_arg_cascade {
        let mut unused: Vec<String> = declared_arg_names
            .iter()
            .filter(|name| !referenced_arg_names.contains(*name))
            .cloned()
            .collect();
        unused.sort();
        for name in unused {
            result.add_warning(format!(
                "{}://{}: unused arg '{}' — declared but never referenced via $(arg) or $(var) in this file",
                package,
                share_path.display(),
                name
            ));
        }
    }

    // <include> inside <group scoped="false"> leaks included-file variables into parent scope.
    for file_expr in &scoped_false_includes {
        let snippet = if file_expr.len() > 60 {
            format!("{}...", &file_expr[..60])
        } else {
            file_expr.clone()
        };
        result.add_warning(format!(
            "{}://{}: <include> inside <group scoped=\"false\"> — '{}' leaks its internal variables into the parent scope; move the <include> out of the group",
            package, share_path.display(), snippet
        ));
    }

    // $(env X) without a fallback crashes at launch time if the variable is unset.
    // Suppressed by default (info) because common env vars like HOME are always set.
    {
        let mut seen = std::collections::HashSet::new();
        for name in &env_no_fallback {
            if seen.insert(name.clone()) {
                result.add_info(format!(
                    "{}://{}: $(env {}) has no fallback — will fail if the environment variable is unset; use $(env {} <default>)",
                    package, share_path.display(), name, name
                ));
            }
        }
    }

    // Preserve result.include_args for API consumers (e.g. external tooling that reads
    // per-include arg contexts) and the renderer (which emits <!-- arg ... --> annotations).
    // process_parsed_file uses LaunchInclude.explicit_args directly and does not need
    // result.include_args, but we keep it populated.
    result.include_args.extend(
        resolved
            .include_args
            .iter()
            .map(|(k, v)| (k.clone(), v.clone())),
    );
    // Convert to unified IR and delegate all common post-processing (accumulation,
    // node chain/source assignment, declared-arg tracking, include recursion,
    // IncludeMarker injection, and excessive-include-arg checks).
    // Note: required_abs_files that couldn't be parsed by extract_file_dependency
    // are reported as errors inside resolved_launch_to_parsed.
    let parsed = resolved_launch_to_parsed(resolved);
    process_parsed_file(
        parsed,
        package,
        share_path,
        &file_path,
        &current_chain,
        &effective_args,
        persisted_arg_context,
        result,
        lockfile,
        locator,
        fetch_dir,
        fetched_packages,
        workflow_options,
        options,
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_py_output(json: &str) -> PyResolverOutput {
        serde_json::from_str(json).unwrap()
    }

    /// Schema fixture — verifies PyResolverOutput deserialization for the full node schema.
    /// Update this test when py_resolver.py's _NodeDict / _ResolverOutput TypedDicts change.
    #[test]
    fn test_py_output_schema_fixture() {
        let json = r#"{
            "packages": ["my_pkg"],
            "includes": [],
            "param_files": [],
            "warnings": [],
            "nodes": [{
                "package": "my_pkg",
                "executable": "my_node",
                "name": "n1",
                "namespace_stack": ["sensing", "lidar"],
                "explicit_namespace": "my_node",
                "parameters": {"rate": "10"},
                "param_files": [],
                "remappings": [["in", "out"]],
                "env": {"ROS_DOMAIN_ID": "42"},
                "kind": "node",
                "plugins": [],
                "target": null
            }]
        }"#;
        let out = parse_py_output(json);
        assert_eq!(out.packages, vec!["my_pkg"]);
        let n = &out.nodes[0];
        assert_eq!(n.namespace_stack, vec!["sensing", "lidar"]);
        assert_eq!(n.explicit_namespace.as_deref(), Some("my_node"));
        assert_eq!(n.parameters["rate"], "10");
        assert_eq!(n.remappings, vec![("in".to_string(), "out".to_string())]);
        assert_eq!(n.env["ROS_DOMAIN_ID"], "42");
    }

    /// Namespace computation — Rust's effective_namespace replaces Python's
    /// _compute_effective_namespace.  Both sides must produce identical results.
    #[test]
    fn test_py_output_namespace_computation() {
        let cases: &[(&[&str], Option<&str>, Option<&str>)] = &[
            (
                &["sensing", "lidar"],
                Some("my_node"),
                Some("/sensing/lidar/my_node"),
            ),
            (&["sensing"], Some("/abs"), Some("/abs")), // absolute overrides stack
            (&[], Some("solo_ns"), Some("/solo_ns")),
            (&["a"], None, Some("/a")),
            (&[], None, None),
        ];
        for (stack, explicit, expected) in cases {
            let stack: Vec<String> = stack.iter().map(|s| s.to_string()).collect();
            let result = crate::resolver::effective_namespace(&stack, *explicit);
            assert_eq!(
                result.as_deref(),
                *expected,
                "stack={stack:?} explicit={explicit:?}"
            );
        }
    }

    /// Container schema fixture — verifies kind/plugins deserialization.
    #[test]
    fn test_py_output_container_schema_fixture() {
        let json = r#"{
            "packages": [], "includes": [], "param_files": [], "warnings": [],
            "nodes": [{
                "package": "rclcpp_components",
                "executable": "component_container",
                "name": "c",
                "namespace_stack": [],
                "explicit_namespace": null,
                "parameters": {}, "param_files": [], "remappings": [], "env": {},
                "kind": "container",
                "plugins": [{
                    "package": "my_pkg",
                    "plugin": "my_pkg::MyPlugin",
                    "name": "plugin1",
                    "parameters": {"a": "1"},
                    "remappings": []
                }],
                "target": null
            }]
        }"#;
        let out = parse_py_output(json);
        let n = &out.nodes[0];
        assert_eq!(n.kind, PyNodeKind::Container);
        assert_eq!(n.plugins[0].plugin, "my_pkg::MyPlugin");
        assert_eq!(n.plugins[0].parameters["a"], "1");
    }
}
