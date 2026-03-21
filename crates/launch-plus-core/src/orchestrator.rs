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
    ComposablePlugin, DependencyKind, EventHandlerKind, FileDependency, IncludeArgContext,
    LaunchInclude, NodeKind, ParsedLaunchFile, ResolvedEventAction, ResolvedNode,
};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
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

    /// Expand `<param from="...">` files at resolve time.
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

    /// Per-file declared arg names and defaults, keyed by (package, share_path).
    ///
    /// Populated during resolution from `parsed.declared_arg_defaults` (XML/YAML)
    /// and `py_output.declared_args` (Python).  Used by:
    /// - the excessive-include-arg check (compares forwarded args against declared keys)
    /// - `--show-args` rendering (shows declared defaults alongside explicit args)
    pub declared_args_by_file: HashMap<(String, PathBuf), HashMap<String, String>>,

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
    let mut failed_repos: HashSet<String> = HashSet::new();

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
    resolve_launch_file(
        lockfile,
        &locator,
        package,
        &share_path,
        fetch_dir,
        options,
        initial_args,
        workflow_options,
        &mut result,
        &mut fetched_packages,
        &mut failed_repos,
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
    lockfile: &Lockfile,
    fetch_dir: &Path,
    persisted_global_params: &[serde_json::Value],
    workflow_options: &ResolveWorkflowOptions,
) -> crate::Result<PyResolverOutput> {
    use std::io::Write;
    use std::process::{Command, Stdio};

    // Write the shim script to a PID-keyed temp file to avoid races when multiple
    // launch-plus processes run concurrently.  Within a single process, the file
    // content is identical (PY_RESOLVER_SCRIPT is constant), so reuse is safe.
    let tmp_dir = std::env::temp_dir().join("launch-plus");
    std::fs::create_dir_all(&tmp_dir)
        .map_err(|e| crate::Error::PythonResolver(format!("failed to create tmp dir: {e}")))?;
    let script_path = tmp_dir.join(format!("py_resolver_{}.py", std::process::id()));
    {
        let mut f = std::fs::File::create(&script_path).map_err(|e| {
            crate::Error::PythonResolver(format!("failed to write py_resolver.py: {e}"))
        })?;
        f.write_all(PY_RESOLVER_SCRIPT.as_bytes()).map_err(|e| {
            crate::Error::PythonResolver(format!("failed to write py_resolver.py: {e}"))
        })?;
    }

    // Inject persisted global_params into args under the reserved key __global_params__.
    // py_resolver strips this key before building the launch context and pre-populates
    // context.launch_configurations["global_params"] from it.
    let mut args_with_globals = initial_args.clone();
    if !persisted_global_params.is_empty() {
        let gp_json = serde_json::to_string(persisted_global_params).map_err(|e| {
            crate::Error::PythonResolver(format!("failed to serialize global_params: {e}"))
        })?;
        args_with_globals.insert("__global_params__".to_string(), gp_json);
    }

    // Build lockfile data for Python: package name → {repo, path, url, version}
    // so py_resolver can fetch missing packages inline via git sparse-checkout.
    let mut lockfile_data: HashMap<String, serde_json::Value> = HashMap::new();
    for (pkg_name, pkg_lock) in &lockfile.packages {
        let Some(repo_lock) = lockfile.repositories.get(&pkg_lock.repo) else {
            tracing::warn!(
                "lockfile integrity: package '{}' references unknown repo '{}'",
                pkg_name,
                pkg_lock.repo
            );
            continue;
        };
        lockfile_data.insert(
            pkg_name.clone(),
            serde_json::json!({
                "repo": pkg_lock.repo,
                "path": pkg_lock.path,
                "url": repo_lock.url,
                "version": repo_lock.version,
            }),
        );
    }

    let script_str = script_path.to_str().ok_or_else(|| {
        crate::Error::PythonResolver("py_resolver script path is not valid UTF-8".to_string())
    })?;
    let file_str = file_path.to_str().ok_or_else(|| {
        crate::Error::PythonResolver(format!(
            "launch file path is not valid UTF-8: {}",
            file_path.display()
        ))
    })?;
    // Pass JSON data via stdin to avoid hitting the OS ARG_MAX limit.
    // Large lockfiles + package_shares can easily exceed the ~2MB argument limit.
    let stdin_payload = serde_json::json!({
        "launch_file": file_str,
        "args": &args_with_globals,
        "package_shares": package_shares,
        "flags": {
            "apply_opaque_file_access": workflow_options.apply_opaque_file_access,
            "preview": workflow_options.preview,
            "inline_params": workflow_options.inline_params,
            "rosdep_fallback": workflow_options.rosdep_fallback,
            "apply_arg_defaults": workflow_options.apply_arg_defaults,
            "global_arg_cascade": workflow_options.global_arg_cascade,
            "allow_unportable_paths": workflow_options.allow_unportable_paths,
            "lockfile_packages": &lockfile_data,
            "fetch_dir": fetch_dir.to_string_lossy(),
        },
    });
    let stdin_json = stdin_payload.to_string();

    let mut child = Command::new("python3")
        .args([script_str])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| crate::Error::PythonResolver(format!("failed to run python3: {e}")))?;

    // Write JSON payload to stdin, then close the pipe so Python sees EOF.
    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        stdin.write_all(stdin_json.as_bytes()).map_err(|e| {
            crate::Error::PythonResolver(format!("failed to write to python3 stdin: {e}"))
        })?;
    }

    let output = child
        .wait_with_output()
        .map_err(|e| crate::Error::PythonResolver(format!("failed to wait for python3: {e}")))?;

    // Clean up the temp script now that the child has exited.
    let _ = std::fs::remove_file(&script_path);

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(crate::Error::PythonResolver(format!(
            "py_resolver failed for {}: {}",
            file_path.display(),
            stderr.trim()
        )));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str::<PyResolverOutput>(&stdout).map_err(|e| {
        crate::Error::PythonResolver(format!(
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
    /// Legacy field — Python now fetches packages inline via _ensure_fetched().
    /// Kept for serde backward compatibility with older py_resolver output.
    #[serde(default)]
    #[allow(dead_code)]
    packages_to_fetch: Vec<String>,
    /// SetLaunchConfiguration calls: {name: value}.
    /// Propagated into the calling XML file's substitution context so that
    /// subsequent $(var name) references resolve correctly.
    #[serde(default)]
    #[allow(dead_code)]
    set_launch_configurations: HashMap<String, String>,
    /// Structured include dependencies: [{package, share_path}].
    /// Extracted by the Python resolver from portable or AMENT install paths,
    /// eliminating the need for reverse path mapping on the Rust side.
    #[serde(default)]
    include_deps: Vec<PyFileDep>,
    /// Structured param file dependencies: [{package, share_path}].
    #[serde(default)]
    param_file_deps: Vec<PyFileDep>,
    /// Event handlers detected in the Python launch file.
    #[serde(default)]
    event_handlers: Vec<PyEventHandler>,
    /// Per-file declared args for --show-args: {"pkg://share_path": [{name, default}]}
    #[serde(default)]
    declared_args_by_file: HashMap<String, Vec<PyDeclaredArg>>,
}

#[derive(Debug, serde::Deserialize, Clone, PartialEq)]
struct PyFileDep {
    package: String,
    share_path: String,
    /// Original path string (for looking up include_args keyed by path).
    #[serde(default)]
    path: String,
    /// Accumulated `PushRosNamespace` stack at the include site in the Python file.
    #[serde(default)]
    namespace_stack: Vec<String>,
    /// Per-entry include args (allows distinct args per include site).
    #[serde(default)]
    include_args: HashMap<String, String>,
}

#[derive(Debug, serde::Deserialize, Default, PartialEq)]
#[serde(rename_all = "snake_case")]
enum PyNodeKind {
    #[default]
    Node,
    Container,
    LoadComposable,
    SetParameter,
    SetRemap,
    Log,
    Executable,
    LifecycleNode,
    EventHandler,
}

/// A deserialized event handler from the Python resolver.
#[derive(Debug, serde::Deserialize)]
struct PyEventHandler {
    handler_kind: String,
    #[serde(default)]
    target: Option<String>,
    #[serde(default)]
    target_node: Option<String>,
    #[serde(default)]
    start_state: Option<String>,
    #[serde(default)]
    goal_state: Option<String>,
    #[serde(default)]
    namespace_stack: Vec<String>,
    #[serde(default)]
    explicit_namespace: Option<String>,
    #[serde(default)]
    actions: Vec<PyEventAction>,
}

#[derive(Debug, serde::Deserialize)]
struct PyEventAction {
    #[serde(default)]
    event: String,
    #[serde(default)]
    target_node: Option<String>,
    /// Kept for serde compatibility; no longer used for namespace computation
    /// (actions inherit the handler's recomputed namespace via apply_parent_namespace).
    #[serde(default)]
    #[allow(dead_code)]
    namespace_stack: Vec<String>,
    #[serde(default)]
    explicit_namespace: Option<String>,
}

/// A param file entry from py_resolver: reference or inlined.
#[derive(Debug, serde::Deserialize)]
struct PyParamFile {
    path: String,
    /// Inlined params as [[key, value], ...]; absent for references.
    #[serde(default)]
    params: Option<Vec<(String, String)>>,
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
    #[serde(default)]
    param_files: Vec<PyParamFile>,
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
    /// Legacy: was used for `kind == SetParameter` (now filtered out).
    #[serde(default)]
    #[allow(dead_code)]
    param_value: Option<String>,
    /// For `kind == Executable`: the resolved command string.
    #[serde(default)]
    cmd: Option<String>,
    /// For `kind == Executable`: whether to run via a shell.
    #[serde(default)]
    shell: bool,
    /// For `kind == SetRemap`: source topic.
    #[serde(default)]
    remap_from: Option<String>,
    /// For `kind == SetRemap`: destination topic.
    #[serde(default)]
    remap_to: Option<String>,
    /// For `kind == Log`: the log message.
    #[serde(default)]
    message: Option<String>,
    /// Include chain from root to this node's source file, as `[package, share_path]` pairs.
    /// Set by py_resolver for nodes within included files; empty for root-level nodes.
    #[serde(default)]
    include_chain: Vec<(String, String)>,
    /// Param files referenced by this node — reference or inlined.
    #[serde(default)]
    param_files: Vec<PyParamFile>,
    /// Resolved `output=` attribute (e.g. "screen", "log", "both").
    #[serde(default)]
    output: Option<String>,
    /// Resolved `arguments=` (CLI args for the node process).
    #[serde(default)]
    args: Option<String>,
    /// Resolved `respawn=` attribute.
    #[serde(default)]
    respawn: Option<String>,
    /// Resolved `respawn_delay=` attribute.
    #[serde(default)]
    respawn_delay: Option<String>,
    /// Event handler kind (for `kind == EventHandler`).
    #[serde(default)]
    handler_kind: Option<String>,
    /// Event handler target node (for `kind == EventHandler`).
    #[serde(default)]
    target_node: Option<String>,
    /// Lifecycle start state (for `kind == EventHandler`).
    #[serde(default)]
    start_state: Option<String>,
    /// Lifecycle goal state (for `kind == EventHandler`).
    #[serde(default)]
    goal_state: Option<String>,
    /// Event handler actions (for `kind == EventHandler`).
    #[serde(default)]
    eh_actions: Vec<PyEventAction>,
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
    failed_repos: &mut HashSet<String>,
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

    // Fail fast: if a previous fetch for a sibling package in the same repo
    // already failed (e.g. Default mode verification error), skip silently.
    if failed_repos.contains(&pkg_lock.repo) {
        return false;
    }

    // Check if already on disk.
    // In clean/default mode, we still need to call fetch_packages so that the
    // repository state is verified (default) or reset (clean) — unless the repo
    // has already been verified/reset for a sibling package in this session.
    let pkg_path = fetch_dir.join(&pkg_lock.repo).join(&pkg_lock.path);
    let repo_already_handled = options.workspace_state != crate::fetcher::WorkspaceState::Dirty
        && fetched_packages
            .iter()
            .any(|p| lockfile.packages.get(p).map(|l| &l.repo) == Some(&pkg_lock.repo));
    if pkg_path.exists()
        && pkg_path.join("package.xml").exists()
        && (options.workspace_state == crate::fetcher::WorkspaceState::Dirty
            || repo_already_handled)
    {
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
            failed_repos.insert(pkg_lock.repo.clone());
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

/// Convert a [`PyParamFile`] from Python output to the renderer's [`ParamFile`].
fn convert_param_file(pf: &PyParamFile) -> crate::resolver::ParamFile {
    if let Some(ref params) = pf.params {
        crate::resolver::ParamFile::Inlined {
            display: pf.path.clone(),
            params: params.clone(),
        }
    } else {
        crate::resolver::ParamFile::Reference {
            display: pf.path.clone(),
            abs: pf.path.clone(), // portable path — Rust doesn't need abs
        }
    }
}

/// Convert a [`PyResolverOutput`] (Python shim output) into the unified [`ParsedLaunchFile`] IR.
///
/// Uses structured `include_deps` / `param_file_deps` emitted by the Python resolver
/// (already decomposed into `(package, share_path)` pairs) — no reverse path mapping needed.
/// Warnings are intentionally excluded here — the caller handles them with py_resolver-specific
/// formatting (package://path: prefix) before calling this function.
fn py_output_to_parsed(py_output: PyResolverOutput) -> ParsedLaunchFile {
    let mut nodes = py_output
        .nodes
        .iter()
        .filter_map(|n| {
            let kind = match n.kind {
                // SetParameter is absorbed into leaf nodes by the Python resolver.
                PyNodeKind::SetParameter => return None,
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
                            param_files: p.param_files.iter().map(convert_param_file).collect(),
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
                            param_files: p.param_files.iter().map(convert_param_file).collect(),
                        })
                        .collect(),
                },
                PyNodeKind::Node => NodeKind::Node,
                PyNodeKind::LifecycleNode => NodeKind::LifecycleNode,
                PyNodeKind::SetRemap => NodeKind::SetRemap {
                    from: n.remap_from.clone().unwrap_or_default(),
                    to: n.remap_to.clone().unwrap_or_default(),
                },
                PyNodeKind::Log => NodeKind::Log {
                    message: n.message.clone().unwrap_or_default(),
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
                PyNodeKind::EventHandler => {
                    let hk_str = n.handler_kind.as_deref().unwrap_or("");
                    let handler_kind = match hk_str {
                        "on_process_start" => EventHandlerKind::OnProcessStart,
                        "on_process_exit" => EventHandlerKind::OnProcessExit,
                        "on_state_transition" => EventHandlerKind::OnStateTransition,
                        "on_shutdown" => EventHandlerKind::OnShutdown,
                        other => {
                            tracing::warn!(
                                "unknown event handler kind from Python resolver: {other}"
                            );
                            return None;
                        }
                    };
                    let handler_ns = crate::resolver::effective_namespace(
                        &n.namespace_stack,
                        n.explicit_namespace.as_deref(),
                    );
                    let actions = n
                        .eh_actions
                        .iter()
                        .map(|a| ResolvedEventAction::EmitEvent {
                            event: a.event.clone(),
                            target_node: a.target_node.clone(),
                            namespace: a.explicit_namespace.clone(),
                        })
                        .collect();
                    NodeKind::EventHandler {
                        handler_kind,
                        target: n.target.clone(),
                        target_node: n.target_node.clone(),
                        namespace: handler_ns,
                        start_state: n.start_state.clone(),
                        goal_state: n.goal_state.clone(),
                        actions,
                    }
                }
            };
            Some(ResolvedNode {
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
                explicit_namespace: n.explicit_namespace.clone(),
                namespace_stack: n.namespace_stack.clone(),
                parameters: n.parameters.clone(),
                remappings: n.remappings.clone(),
                env: n.env.clone(),
                source: if n.include_chain.is_empty() {
                    None // root-level node — source set by process_parsed_file
                } else {
                    n.include_chain
                        .last()
                        .map(|(pkg, path)| (pkg.clone(), PathBuf::from(path)))
                },
                include_chain: n
                    .include_chain
                    .iter()
                    .map(|(pkg, path)| (pkg.clone(), PathBuf::from(path)))
                    .collect(),
                kind,
                param_files: n.param_files.iter().map(convert_param_file).collect(),
                output: n.output.clone(),
                args: n.args.clone(),
                respawn: n.respawn.clone(),
                respawn_delay: n.respawn_delay.clone(),
            })
        })
        .collect::<Vec<_>>();

    // Legacy: convert event handlers from the separate `event_handlers` field
    // for backward compatibility. Skip if the nodes list already contains
    // interleaved event handlers (new behavior).
    let has_inline_eh = nodes
        .iter()
        .any(|n| matches!(n.kind, NodeKind::EventHandler { .. }));
    if !has_inline_eh {
        for eh in &py_output.event_handlers {
            let handler_kind = match eh.handler_kind.as_str() {
                "on_process_start" => EventHandlerKind::OnProcessStart,
                "on_process_exit" => EventHandlerKind::OnProcessExit,
                "on_state_transition" => EventHandlerKind::OnStateTransition,
                "on_shutdown" => EventHandlerKind::OnShutdown,
                other => {
                    tracing::warn!("unknown event handler kind from Python resolver: {other}");
                    continue;
                }
            };
            let handler_ns = crate::resolver::effective_namespace(
                &eh.namespace_stack,
                eh.explicit_namespace.as_deref(),
            );
            let actions = eh
                .actions
                .iter()
                .map(|a| ResolvedEventAction::EmitEvent {
                    event: a.event.clone(),
                    target_node: a.target_node.clone(),
                    namespace: a.explicit_namespace.clone(),
                })
                .collect();
            nodes.push(ResolvedNode {
                namespace_stack: eh.namespace_stack.clone(),
                explicit_namespace: eh.explicit_namespace.clone(),
                kind: NodeKind::EventHandler {
                    handler_kind,
                    target: eh.target.clone(),
                    target_node: eh.target_node.clone(),
                    namespace: handler_ns,
                    start_state: eh.start_state.clone(),
                    goal_state: eh.goal_state.clone(),
                    actions,
                },
                ..ResolvedNode::default()
            });
        }
    }

    let launch_includes = py_output
        .include_deps
        .iter()
        .map(|dep| {
            // Prefer per-entry include_args (correct when the same file is
            // included multiple times with different args); fall back to the
            // shared include_args dict for backward compatibility.
            let explicit_args = if !dep.include_args.is_empty() {
                dep.include_args.clone()
            } else {
                py_output
                    .include_args
                    .get(&dep.path)
                    .cloned()
                    .unwrap_or_default()
            };
            LaunchInclude {
                package: dep.package.clone(),
                share_path: PathBuf::from(&dep.share_path),
                explicit_args,
                namespace_stack: dep.namespace_stack.clone(),
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

    // Convert per-file declared args from Python's "pkg://share_path" keyed format.
    let mut declared_args_by_file = HashMap::new();
    for (key_str, args) in &py_output.declared_args_by_file {
        // Parse "pkg://share_path" format
        let (pkg, share_path) = if let Some(idx) = key_str.find("://") {
            (
                key_str[..idx].to_string(),
                PathBuf::from(&key_str[idx + 3..]),
            )
        } else {
            (String::new(), PathBuf::from(key_str))
        };
        let arg_map: HashMap<String, String> = args
            .iter()
            .map(|a| (a.name.clone(), a.default.clone()))
            .collect();
        declared_args_by_file.insert((pkg, share_path), arg_map);
    }

    ParsedLaunchFile {
        packages: py_output.packages,
        nodes,
        launch_includes,
        param_files,
        other_files: Vec::new(),
        declared_arg_defaults,
        declared_args_by_file,
        global_params: py_output.global_params,
        warnings: Vec::new(), // warnings emitted separately by the caller with py_resolver formatting
        errors: Vec::new(),
        infos: Vec::new(),
    }
}

/// Common post-processing for any parsed launch file (XML, YAML, or Python).
///
/// Accumulates packages/files/nodes into `result` and updates declared-arg tracking.
/// The Python resolver handles all includes inline, so this function only records
/// dependency edges without recursing.
fn process_parsed_file(
    parsed: ParsedLaunchFile,
    package: &str,
    share_path: &Path,
    file_path: &Path,
    current_chain: &[(String, PathBuf)],
    result: &mut ResolveResult,
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
    // If Python already set include_chain (non-empty), prepend current_chain.
    // Otherwise, use current_chain as the full chain (root-level nodes).
    let source = (package.to_string(), share_path.to_path_buf());
    for mut node in parsed.nodes {
        if !node.include_chain.is_empty() {
            // Python set the chain — prepend current_chain (which includes the root)
            let mut full_chain = current_chain.to_vec();
            full_chain.extend(node.include_chain);
            node.include_chain = full_chain;
            // source is already set by py_output_to_parsed from the last chain entry
        } else if matches!(node.kind, NodeKind::IncludeMarker) {
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

    // Store declared args for --show-args.
    // Prefer per-file mapping; ensure the root file has an entry (empty if it
    // declared no args).  Using declared_arg_defaults for the root key would
    // incorrectly attribute included files' args to the root when the root
    // declares no args itself.
    let root_key = (package.to_string(), share_path.to_path_buf());
    let root_args = parsed
        .declared_args_by_file
        .get(&root_key)
        .cloned()
        .unwrap_or_default();
    result.declared_args_by_file.insert(root_key, root_args);
    for (key, args) in &parsed.declared_args_by_file {
        result
            .declared_args_by_file
            .insert(key.clone(), args.clone());
    }

    // Track launch include dependencies (for fetching/building) and populate
    // include_args for the renderer's --show-args annotations.
    // The Python resolver already inlines all included files' nodes, so we do NOT
    // re-resolve them here — only record the dependency edges.
    for include in parsed.launch_includes {
        let key = (include.package.clone(), include.share_path.clone());
        // Populate include_args for --show-args rendering.
        if let Some(existing) = result.include_args.get(&key) {
            // Detect divergent argument contexts for the same included file.
            if existing.explicit != include.explicit_args
                || existing.namespace_stack != include.namespace_stack
            {
                warn!(
                    "File {:?} from package {:?} is included multiple times with different \
                     argument contexts; --show-args will use the first include site's args \
                     and namespace stack.",
                    key.1, key.0
                );
            }
        } else {
            result.include_args.insert(
                key.clone(),
                IncludeArgContext {
                    explicit: include.explicit_args.clone(),
                    with_cascade: HashMap::new(),
                    namespace_stack: include.namespace_stack.clone(),
                },
            );
        }
        if !result
            .launch_files
            .iter()
            .any(|f| f.package == include.package && f.share_path == include.share_path)
        {
            result.launch_files.push(FileDependency {
                package: include.package,
                share_path: include.share_path,
                kind: DependencyKind::Launch,
            });
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
    result: &mut ResolveResult,
    fetched_packages: &mut HashSet<String>,
    failed_repos: &mut HashSet<String>,
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
                failed_repos,
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

    // Snapshot the globally accumulated SetParameter values accumulated so far.
    // This file may depend on SetParameter calls from sibling/ancestor files (e.g.
    // vehicle_info.launch.py setting front_overhang before ground_segmentation.launch.py
    // reads it).  The snapshot is passed to py_resolver via __global_params__ so that
    // OpaqueFunction bodies can read cross-file parameters.
    let current_global_params = result.global_params.clone();

    // Run the Python resolver shim.  Python handles package fetching inline via
    // _ensure_fetched() — no retry loop needed.
    let py_output = match run_py_resolver(
        &file_path,
        initial_args,
        &package_shares,
        lockfile,
        fetch_dir,
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

    // Build include chain for this file as (package, share_path) pairs.
    let mut current_chain = parent_chain;
    current_chain.push((package.to_string(), share_path.to_path_buf()));

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
        result,
    );
}

/// Resolve a single launch file by routing it to the Python resolver.
///
/// Python handles all parsing, substitution resolution, and include
/// traversal internally.  This function is the Rust→Python bridge.
fn resolve_launch_file(
    lockfile: &Lockfile,
    locator: &PackageLocator,
    package: &str,
    share_path: &Path,
    fetch_dir: &Path,
    options: &FetchOptions,
    initial_args: HashMap<String, String>,
    workflow_options: &ResolveWorkflowOptions,
    result: &mut ResolveResult,
    fetched_packages: &mut HashSet<String>,
    failed_repos: &mut HashSet<String>,
) {
    debug!(
        "Resolving launch file: {}:{}",
        package,
        share_path.display()
    );

    // Route ALL file types through the Python resolver.
    // py_resolver.py detects the format by extension and handles XML/YAML/Python
    // uniformly, including cross-format includes and cycle detection.
    if is_python_launch_file(share_path)
        || is_xml_launch_file(share_path)
        || share_path
            .extension()
            .is_some_and(|e| matches!(e.to_str(), Some("yaml" | "yml")))
    {
        resolve_python_file_recursive(
            lockfile,
            locator,
            package,
            share_path,
            fetch_dir,
            options,
            &initial_args,
            workflow_options,
            result,
            fetched_packages,
            failed_repos,
            vec![], // root-level: no parent chain
        );
        return;
    }

    // Unsupported file type
    debug!(
        "Skipping unsupported launch file type: {}:{}",
        package,
        share_path.display()
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
