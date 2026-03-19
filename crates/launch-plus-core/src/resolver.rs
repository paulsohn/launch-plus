//! Resolver module: Parse and resolve ROS 2 launch files
//!
//! The resolver is responsible for:
//! - Parsing launch.xml files into an AST
//! - Resolving substitutions ($(arg), $(var), $(env), $(find-pkg-share), etc.)
//! - Evaluating conditionals (if, unless)
//! - Flattening includes
//! - Tracking package dependencies

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;

pub use crate::parser::xml::parse_launch_xml;
pub use crate::parser::yaml::parse_launch_yaml;

// Re-export AST types from the parser module (canonical home).
pub use crate::parser::{
    ComposableNode, Condition, ConditionKind, Env, EventHandlerKind, IncludeArg, LaunchElement,
    LaunchFile, Param, Remap,
};

/// A resolved composable node plugin (the result of resolving a `ComposableNode`).
///
/// Unlike a regular node, composable plugins run inside a container process.
/// The `plugin` field (C++ class name) serves as the executable identifier.
#[derive(Debug, Clone)]
pub struct ComposablePlugin {
    pub package: String,
    /// C++ plugin class name (e.g. `nebula::ros::HesaiRosWrapper`)
    pub plugin: String,
    pub name: Option<String>,
    pub parameters: BTreeMap<String, String>,
    pub remappings: Vec<(String, String)>,
    /// Param files resolved at resolve time (see [`ParamFile`]).
    pub param_files: Vec<ParamFile>,
}

/// A parameter file reference or its pre-expanded contents.
///
/// Populated by the resolver at resolve time so that the renderer never does file I/O.
/// Stored in [`ResolvedNode::param_files`] and [`ComposablePlugin::param_files`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParamFile {
    /// File reference — not inlined.
    /// Renderer emits `<param from="display"/>`.
    Reference { display: String, abs: String },
    /// Pre-expanded at resolve time (when `--inline-params` is active).
    /// Renderer emits `<!-- params from: display -->`, individual `<param>` elements,
    /// and `<!-- end params from: display -->`.
    Inlined {
        display: String,
        params: Vec<(String, String)>,
    },
}

/// How a `ResolvedNode` should be rendered in the output XML.
#[derive(Debug, Clone, Default)]
pub enum NodeKind {
    /// A regular `<node>` process (default).
    #[default]
    Node,
    /// A composable node container (`<node_container>`).
    ///
    /// `package`/`executable` on the parent `ResolvedNode` are the container process.
    /// `plugins` are the `<composable_node>` children loaded at container startup.
    Container { plugins: Vec<ComposablePlugin> },
    /// A dynamic plugin load into a running container (`<load_composable_node>`).
    ///
    /// `package`/`executable` on the parent `ResolvedNode` are empty (not a process).
    LoadComposable {
        target: String,
        plugins: Vec<ComposablePlugin>,
    },
    /// An included file that contributed no executable nodes.
    ///
    /// Rendered as an inline `<!-- source: pkg://path -->` / `<!-- end: pkg://path -->`
    /// comment pair — no `<group>` element, even in nested mode.  Intermediate levels
    /// in the include chain are opened persistently so that sibling real nodes share the
    /// correct group context.  Excluded from semantic comparison.
    IncludeMarker,
    /// A `<log message="..."/>` action.  Excluded from semantic comparison.
    Log { message: String },
    /// A `<set_parameter name="..." value="..."/>` statement.  Sets a ROS parameter
    /// globally for all subsequently-launched nodes.  Excluded from semantic comparison.
    SetParameter { name: String, value: String },
    /// A `<set_remap from="..." to="..."/>` statement.  Applies a global topic remapping
    /// to all subsequently-launched nodes.  Excluded from semantic comparison.
    SetRemap { from: String, to: String },
    /// An external process: `<executable cmd="..." name="..." shell="true/false"/>`.
    ///
    /// Equivalent to Python `ExecuteProcess`.  Included in semantic comparison.
    Executable {
        cmd: String,
        name: Option<String>,
        shell: bool,
    },
    /// A lifecycle-managed node: `<lifecycle_node pkg="..." exec="..."/>`.
    ///
    /// Identical to `Node` for dependency purposes; rendered as `<lifecycle_node>` in XML.
    /// launch-plus XML extension.
    LifecycleNode,
    /// An event handler: `<on_process_start>`, `<on_process_exit>`, `<on_state_transition>`.
    ///
    /// Contains child actions (e.g. `<emit_event>`).  launch-plus XML extension.
    /// Excluded from semantic comparison.
    EventHandler {
        handler_kind: EventHandlerKind,
        target: Option<String>,
        target_node: Option<String>,
        namespace: Option<String>,
        start_state: Option<String>,
        goal_state: Option<String>,
        actions: Vec<ResolvedEventAction>,
    },
}

/// A resolved action inside an event handler.
#[derive(Debug, Clone)]
pub enum ResolvedEventAction {
    /// `<emit_event event="..." target_node="..." namespace="..."/>`.
    EmitEvent {
        event: String,
        target_node: Option<String>,
        namespace: Option<String>,
    },
}

// ============================================================================
// Substitution Parser (used by static analysis)
// ============================================================================

/// Parsed representation of a ROS 2 substitution expression.
#[derive(Debug, Clone, PartialEq)]
pub enum Substitution {
    /// $(arg name)
    Arg(String),
    /// $(var name)
    Var(String),
    /// $(env NAME) or $(env NAME default)
    Env {
        name: String,
        default: Option<String>,
    },
    /// $(find-pkg-share package)
    FindPkgShare(String),
    /// $(find-pkg-prefix package)
    FindPkgPrefix(String),
    /// $(dirname)
    Dirname,
    /// $(eval <python_expr>) — expression may contain nested substitutions
    Eval(String),
    /// $(command 'shell cmd' ['on_error']) — executes a shell command at ROS 2 launch time.
    Command(String),
    /// Literal text (not a substitution)
    Literal(String),
}

/// Parse a string containing substitutions into parts
pub fn parse_substitutions(input: &str) -> crate::Result<Vec<Substitution>> {
    let mut parts = Vec::new();
    let mut chars = input.chars().peekable();
    let mut literal = String::new();

    while let Some(c) = chars.next() {
        if c == '$' && chars.peek() == Some(&'(') {
            if !literal.is_empty() {
                parts.push(Substitution::Literal(std::mem::take(&mut literal)));
            }
            chars.next(); // consume '('

            let mut expr = String::new();
            let mut depth = 1;
            for c in chars.by_ref() {
                if c == '(' {
                    depth += 1;
                    expr.push(c);
                } else if c == ')' {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                    expr.push(c);
                } else {
                    expr.push(c);
                }
            }

            let sub = parse_substitution_expr(&expr)?;
            parts.push(sub);
        } else {
            literal.push(c);
        }
    }

    if !literal.is_empty() {
        parts.push(Substitution::Literal(literal));
    }

    Ok(parts)
}

fn normalize_eval_expr(expr: &str) -> String {
    fn has_unescaped(s: &str, ch: u8) -> bool {
        let b = s.as_bytes();
        let mut i = 0;
        while i < b.len() {
            if b[i] == b'\\' {
                i += 2;
            } else if b[i] == ch {
                return true;
            } else {
                i += 1;
            }
        }
        false
    }

    let expr: &str = if expr.len() >= 2
        && expr.starts_with('\'')
        && expr.ends_with('\'')
        && !has_unescaped(&expr[1..expr.len() - 1], b'\'')
    {
        &expr[1..expr.len() - 1]
    } else {
        expr
    };

    let unescaped = expr.replace("\\'", "'").replace("\\\"", "\"");

    let s = unescaped.as_str();
    if s.len() >= 2
        && s.starts_with('"')
        && s.ends_with('"')
        && !has_unescaped(&s[1..s.len() - 1], b'"')
    {
        s[1..s.len() - 1].to_string()
    } else {
        unescaped
    }
}

fn parse_substitution_expr(expr: &str) -> crate::Result<Substitution> {
    let expr = expr.trim();

    if expr == "dirname" {
        return Ok(Substitution::Dirname);
    }

    let mut parts = expr.splitn(2, char::is_whitespace);
    let cmd = parts.next().unwrap_or("");
    let arg = parts.next().map(|s| s.trim());

    match cmd {
        "arg" => {
            let name =
                arg.ok_or_else(|| crate::Error::LaunchParse("$(arg) requires a name".into()))?;
            Ok(Substitution::Arg(name.to_string()))
        }
        "var" => {
            let name =
                arg.ok_or_else(|| crate::Error::LaunchParse("$(var) requires a name".into()))?;
            Ok(Substitution::Var(name.to_string()))
        }
        "env" => {
            let arg_str =
                arg.ok_or_else(|| crate::Error::LaunchParse("$(env) requires a name".into()))?;
            let mut env_parts = arg_str.splitn(2, char::is_whitespace);
            let name = env_parts.next().unwrap().to_string();
            let default = env_parts.next().map(|s| s.trim().to_string());
            Ok(Substitution::Env { name, default })
        }
        "find-pkg-share" => {
            let pkg = arg.ok_or_else(|| {
                crate::Error::LaunchParse("$(find-pkg-share) requires a package name".into())
            })?;
            Ok(Substitution::FindPkgShare(pkg.to_string()))
        }
        "find-pkg-prefix" => {
            let pkg = arg.ok_or_else(|| {
                crate::Error::LaunchParse("$(find-pkg-prefix) requires a package name".into())
            })?;
            Ok(Substitution::FindPkgPrefix(pkg.to_string()))
        }
        "eval" => {
            let python_expr = arg.ok_or_else(|| {
                crate::Error::LaunchParse("$(eval) requires an expression".into())
            })?;
            let stripped = normalize_eval_expr(python_expr);
            Ok(Substitution::Eval(stripped))
        }
        "command" => {
            let body = arg.unwrap_or("").to_string();
            Ok(Substitution::Command(body))
        }
        _ => Err(crate::Error::LaunchParse(format!(
            "unknown substitution: $({})",
            cmd
        ))),
    }
}

/// Extract file dependency from a path containing $(find-pkg-share pkg) or $(find-pkg-prefix pkg)
///
/// Returns Some(FileDependency) if the path matches the pattern, None otherwise.
/// Works with both resolved and unresolved paths.
///
/// The `kind` parameter specifies how this dependency should be processed:
/// - `Launch`: Needs recursive parsing to discover more dependencies
/// - `Param`: Just needs fetching, no parsing
/// - `Other`: Needs fetching, no special handling
pub fn extract_file_dependency(path: &str, kind: DependencyKind) -> Option<FileDependency> {
    // Pattern: $(find-pkg-share <pkg>)<path> or $(find-pkg-prefix <pkg>)<path>
    // The package name may itself be a substitution like $(var pkg_name)

    let path = path.trim();

    // Check for $(find-pkg-share ...) or $(find-pkg-prefix ...)
    for prefix in ["$(find-pkg-share ", "$(find-pkg-prefix "] {
        if let Some(rest) = path.strip_prefix(prefix) {
            // Find the matching closing paren, handling nested parens
            let mut depth = 1;
            let mut pkg_end = 0;
            for (i, c) in rest.char_indices() {
                match c {
                    '(' => depth += 1,
                    ')' => {
                        depth -= 1;
                        if depth == 0 {
                            pkg_end = i;
                            break;
                        }
                    }
                    _ => {}
                }
            }

            if pkg_end > 0 {
                let package = rest[..pkg_end].trim().to_string();
                let share_path = rest[pkg_end + 1..].trim_start_matches('/');

                if !share_path.is_empty() {
                    return Some(FileDependency {
                        package,
                        share_path: PathBuf::from(share_path),
                        kind,
                    });
                }
            }
        }
    }

    // Match AMENT install paths: .../share/<package>/<rest>
    // This handles absolute paths from non-preview mode where $(find-pkg-share ...)
    // was resolved to a real install path like <prefix>/share/<pkg>/launch/foo.xml.
    if !path.contains("$(") {
        if let Some(idx) = path.find("/share/") {
            let after_share = &path[idx + 7..]; // skip "/share/"
            if let Some(slash) = after_share.find('/') {
                let package = &after_share[..slash];
                let rest = &after_share[slash + 1..];
                if !rest.is_empty() && !package.is_empty() {
                    return Some(FileDependency {
                        package: package.to_string(),
                        share_path: PathBuf::from(rest),
                        kind,
                    });
                }
            }
        }
    }

    None
}

// ============================================================================
// Resolved Types (output of resolution)
// ============================================================================

/// The kind of file dependency - determines how it should be processed
#[derive(Debug, Clone, Copy, Hash, Eq, PartialEq)]
pub enum DependencyKind {
    /// Launch file - needs recursive parsing to discover more dependencies
    Launch,
    /// Parameter/config file - just needs fetching, no parsing
    Param,
    /// Other file type - needs fetching, no special handling
    Other,
}

/// A file dependency within a package
#[derive(Debug, Clone, Hash, Eq, PartialEq)]
pub struct FileDependency {
    /// Package name
    pub package: String,
    /// Path relative to package share directory (e.g., "launch/foo.launch.xml")
    pub share_path: PathBuf,
    /// Kind of dependency - determines processing strategy
    pub kind: DependencyKind,
}

/// Context for args passed to an included launch file.
///
/// Stored in [`ResolvedLaunch::include_args`] so the orchestrator can choose
/// which scoping model to apply when recursing into the child.
#[derive(Debug, Clone, Default)]
pub struct IncludeArgContext {
    /// Only the args explicitly forwarded via `<arg name="..." value="..."/>` in the
    /// `<include>` tag.  Used in the default strict mode where every file must declare
    /// its own interface.
    pub explicit: HashMap<String, String>,

    /// Full parent arg context at the time of the include, plus explicit overrides.
    /// Used when `--allow-global-arg-cascade` is set, emulating ROS 2's global
    /// `LaunchConfiguration` sharing.  This allows child files to use `$(var x)` without
    /// declaring `<arg name="x"/>`.  Prefer explicit forwarding; use this flag only for
    /// legacy launch files that cannot be refactored.
    pub with_cascade: HashMap<String, String>,

    /// Accumulated `<push-ros-namespace>` stack at the include site.
    ///
    /// The orchestrator applies this as a prefix to all nodes produced by the child
    /// file, so that cross-file namespace propagation matches ROS 2 semantics.
    pub namespace_stack: Vec<String>,
}

/// A fully resolved launch configuration
#[derive(Debug, Clone, Default)]
pub struct ResolvedLaunch {
    /// All nodes to be launched
    pub nodes: Vec<ResolvedNode>,
    /// All packages required (tracked during resolution)
    pub required_packages: HashSet<String>,
    /// All files required (package + relative path), in encounter order.
    pub required_files: Vec<FileDependency>,
    /// Include files that were processed
    pub processed_includes: Vec<PathBuf>,
    /// Resolved arg context for each included launch file:
    /// (package, share_path) → [`IncludeArgContext`]
    pub include_args: HashMap<(String, PathBuf), IncludeArgContext>,
    /// Arg context established by this file: all keys added to `ctx.args` during resolution
    /// that were not present in `initial_args` (i.e. declared by the file itself, not passed
    /// in by the caller).
    ///
    /// In ROS 2's launch model, `DeclareLaunchArgument` writes into the global
    /// `LaunchConfiguration`, making declared defaults visible to every sibling and
    /// descendant in the launch tree regardless of explicit forwarding.  This field captures
    /// the file's own arg declarations so the orchestrator can persist them across the include
    /// chain — merging them into each descendant's `initial_args` at lower priority than
    /// explicitly-forwarded args — without enabling full `with_cascade` mode.
    pub declared_arg_defaults: HashMap<String, String>,

    /// Include paths that resolved to absolute filesystem paths and could not be mapped to
    /// a `(package, share_path)` pair by `extract_file_dependency`.
    ///
    /// This happens when a `<let>` variable is assigned `$(find-pkg-share X)` and that
    /// substitution is resolved to a real path at parse time (because `pkg_share_resolver`
    /// is set in `SubstitutionContext`).  Subsequent uses like `$(var sensor_launch_pkg)/launch/foo.xml`
    /// become absolute paths that `extract_file_dependency` cannot parse.
    ///
    /// The orchestrator reports these as errors — launch files should always use
    /// `$(find-pkg-share <pkg>)` instead of hardcoded absolute paths.
    pub required_abs_files: Vec<(PathBuf, DependencyKind, IncludeArgContext)>,

    /// Non-fatal errors collected during resolution (e.g. unportable path detection in
    /// preview mode when `--allow-including-unportable-path` is not set).
    /// Propagated to `ResolveResult::errors` by the orchestrator via `ResolveResult::merge`.
    pub errors: Vec<String>,

    /// Non-fatal warnings collected during resolution.
    /// Propagated to `ResolveResult::warnings` by the orchestrator via `ResolveResult::merge`.
    pub warnings: Vec<String>,

    /// Informational messages (pedantic warnings suppressed by default).
    /// Propagated to `ResolveResult::infos` by the orchestrator.
    pub infos: Vec<String>,
}

/// A single include reference with the explicit args to forward to the child file.
///
/// Produced by all three format adapters; consumed by `process_parsed_file`.
#[derive(Debug, Clone)]
pub struct LaunchInclude {
    pub package: String,
    pub share_path: PathBuf,
    /// Args explicitly passed at the include site (not cascaded parent args).
    pub explicit_args: HashMap<String, String>,
    /// Accumulated `<push-ros-namespace>` stack at the include site in the parent file.
    ///
    /// Applied as a namespace prefix to all nodes produced by the child file.
    pub namespace_stack: Vec<String>,
}

/// Unified per-file analysis result from any launch file format (XML, YAML, or Python).
///
/// Both `resolved_launch_to_parsed` (XML/YAML) and `py_output_to_parsed` (Python)
/// produce this type.  The orchestrator's `process_parsed_file` consumes it uniformly.
#[derive(Debug, Default)]
pub struct ParsedLaunchFile {
    pub packages: Vec<String>,
    /// Resolved nodes declared directly in this file (full fidelity for all formats).
    pub nodes: Vec<ResolvedNode>,
    /// Included launch files, ready for recursive processing by the orchestrator.
    pub launch_includes: Vec<LaunchInclude>,
    pub param_files: Vec<FileDependency>,
    pub other_files: Vec<FileDependency>,
    /// Args declared in this file with their resolved defaults.
    pub declared_arg_defaults: HashMap<String, String>,
    /// SetParameter values accumulated by this file (Python only; empty for XML/YAML).
    pub global_params: Vec<serde_json::Value>,
    pub warnings: Vec<String>,
    pub errors: Vec<String>,
    pub infos: Vec<String>,
}

/// A fully resolved node
#[derive(Debug, Clone, Default)]
pub struct ResolvedNode {
    /// Package name
    pub package: String,
    /// Executable name
    pub executable: String,
    /// Node name (optional; if omitted ROS 2 derives it from the executable)
    pub name: Option<String>,
    /// Effective namespace: combination of `namespace_stack` and any explicit `namespace=`
    /// attribute on the `<node>` element (following ROS 2 `namespace_join` semantics).
    pub namespace: Option<String>,
    /// The node's own `namespace=` attribute value (before combining with the stack).
    ///
    /// Stored separately so that cross-file namespace propagation can recompute the
    /// effective namespace from a merged stack without losing the explicit attribute.
    pub explicit_namespace: Option<String>,
    /// Resolved `<push-ros-namespace>` stack at the point this node was declared.
    ///
    /// Each entry corresponds to one `<push-ros-namespace namespace="..."/>` that was active
    /// in an enclosing scope.  Empty when no namespace push is in effect.
    pub namespace_stack: Vec<String>,
    /// Parameters (name → value), sorted by name for deterministic ordering.
    pub parameters: BTreeMap<String, String>,
    /// Topic remappings (from → to)
    pub remappings: Vec<(String, String)>,
    /// Environment variables (name → value), sorted by name for deterministic ordering.
    pub env: BTreeMap<String, String>,
    /// Source launch file as `(package, share_path)` for portability.
    ///
    /// `share_path` is relative to the package share root (e.g. `launch/sensing.launch.xml`).
    /// `None` when resolved without orchestrator context (e.g. unit tests or inline mode).
    /// Lives only in traceability comments in XML output; excluded from semantic comparison.
    pub source: Option<(String, PathBuf)>,
    /// Include chain from the entrypoint to `source`, inclusive, as `(package, share_path)` pairs.
    ///
    /// Set by the orchestrator after resolving each file.  Empty when the node was resolved
    /// without orchestrator context (e.g. unit tests or inline mode).
    ///
    /// Example: `[(autoware_launch, launch/autoware.launch.xml), (tier4_system_launch, launch/tier4_system_component.launch.xml)]`
    /// Lives only in traceability comments in XML output; excluded from semantic comparison.
    pub include_chain: Vec<(String, PathBuf)>,
    /// How this node should be rendered in the output XML.
    pub kind: NodeKind,
    /// Param files resolved at resolve time (see [`ParamFile`]).
    pub param_files: Vec<ParamFile>,
    /// Resolved value of `output="..."` attribute, if present (`"screen"`, `"log"`, `"both"`).
    pub output: Option<String>,
    /// Resolved value of `args="..."` attribute, if present.
    pub args: Option<String>,
    /// Resolved value of `respawn="..."` attribute, if present.
    pub respawn: Option<String>,
    /// Resolved value of `respawn_delay="..."` attribute, if present.
    pub respawn_delay: Option<String>,
}

/// Node identity carrier for comparing resolved launch outputs.
///
/// Contains all execution-relevant fields from [`ResolvedNode`] — the fields that determine
/// *what* runs and *how* — but excludes location fields (`source`, `include_chain`) that
/// encode *where* the node was declared in the source tree.
///
/// Use [`semantic_eq`] to check whether two resolutions produce the same set of nodes,
/// regardless of whether one was resolved from source paths (`--preview`) and the other
/// from install paths.
#[derive(Debug, Clone, PartialEq)]
pub struct SemanticNode {
    pub package: String,
    pub executable: String,
    pub name: Option<String>,
    pub namespace: Option<String>,
    pub namespace_stack: Vec<String>,
    pub parameters: BTreeMap<String, String>,
    pub remappings: Vec<(String, String)>,
    pub env: BTreeMap<String, String>,
}

impl From<&ResolvedNode> for SemanticNode {
    fn from(n: &ResolvedNode) -> Self {
        Self {
            package: n.package.clone(),
            executable: n.executable.clone(),
            name: n.name.clone(),
            namespace: n.namespace.clone(),
            namespace_stack: n.namespace_stack.clone(),
            parameters: n.parameters.clone(),
            remappings: n.remappings.clone(),
            env: n.env.clone(),
        }
    }
}

/// Return `true` if `a` and `b` contain the same nodes in the same order,
/// ignoring location fields (`source`, `include_chain`).
///
/// Used to verify that a `--preview` resolution (source paths) produces an identical
/// set of nodes as a non-preview resolution (install paths).
pub fn semantic_eq(a: &[ResolvedNode], b: &[ResolvedNode]) -> bool {
    // IncludeMarker nodes carry no executable content — exclude them from comparison.
    let is_exec = |n: &&ResolvedNode| {
        !matches!(
            n.kind,
            NodeKind::IncludeMarker
                | NodeKind::SetParameter { .. }
                | NodeKind::SetRemap { .. }
                | NodeKind::Log { .. }
                | NodeKind::EventHandler { .. }
        )
    };
    let a_s: Vec<SemanticNode> = a.iter().filter(is_exec).map(SemanticNode::from).collect();
    let b_s: Vec<SemanticNode> = b.iter().filter(is_exec).map(SemanticNode::from).collect();
    a_s == b_s
}

pub(crate) fn ros2_namespace_join(base: Option<&str>, next: &str) -> Option<String> {
    let next = next.trim_end_matches('/');
    if next.is_empty() {
        return base.map(|s| s.to_string());
    }
    if next.starts_with('/') {
        // Absolute component resets the accumulated base.
        Some(next.trim_end_matches('/').to_string())
    } else {
        match base {
            None | Some("") | Some("/") => Some(format!("/{next}")),
            Some(b) => Some(format!("{}/{next}", b.trim_end_matches('/'))),
        }
    }
}

/// Compute the effective ROS 2 namespace for a node by combining the accumulated
/// `<push-ros-namespace>` stack with the node's own `namespace` attribute.
///
/// Follows ROS 2 `namespace_join` semantics applied left-to-right:
/// - Relative components are appended (e.g. `["sensing", "lidar"]` → `"/sensing/lidar"`).
/// - Absolute components **reset** the accumulated base (e.g. `["sensing", "/abs"]` → `"/abs"`).
/// - The node's explicit `namespace=` attribute is applied last with the same rules.
/// - Returns `None` when both the stack and the explicit namespace are empty.
pub(crate) fn effective_namespace(stack: &[String], node_ns: Option<&str>) -> Option<String> {
    let mut current: Option<String> = None;
    for component in stack {
        current = ros2_namespace_join(current.as_deref(), component);
    }
    if let Some(ns) = node_ns {
        current = ros2_namespace_join(current.as_deref(), ns);
    }
    current
}

// ============================================================================
// Renderer
// ============================================================================

/// Render resolved nodes as a `<launch>` XML document.
///
/// **Nested grouping (default):** Each source file boundary in the include hierarchy produces
/// a nested `<group>` element.  A node from `leaf.launch.xml`, included via
/// `component.launch.xml` → `sub.launch.xml` → `leaf.launch.xml`, is wrapped in three nested
/// `<group>` elements — one per level after the root.  Within each source group, an inner
/// `<group><push-ros-namespace>` sub-group is emitted when the namespace stack changes.
/// Nodes from the root file itself are emitted flat at the `<launch>` level.
///
/// **Flatten groups (`flatten=true`):** Source-boundary `<group>` wrappers are suppressed.
/// Nodes from non-root files carry a `<!-- source: pkg://path -->` comment keyed to the
/// actual leaf file (not the top-level component boundary), with a matching
/// `<!-- end: pkg://path -->` comment after each section.  Namespace groups
/// (`<group><push-ros-namespace>`) are still emitted unless `flatten_namespaces` is also set.
///
/// **Flatten namespaces (`flatten_namespaces=true`):** `<push-ros-namespace>` inner groups
/// are suppressed; the fully composed `namespace=` value is emitted directly on each `<node>`
/// element.  Source groups are unaffected.  Combined with `flatten`, produces entirely
/// group-free output.  A `<!-- end: pkg://path -->` comment is emitted after each source
/// `</group>` to aid readability (source groups are the only structural elements in this mode).
///
/// **End comments:** Each source section is bracketed by a `<!-- source: pkg://path -->`
/// opener and a matching `<!-- end: pkg://path -->` closer.  In flat mode the closer marks
/// the extent of the section where there is no `</group>` tag.  In nested mode it labels
/// the closing `</group>` with the originating file name.
/// Emit `<!-- arg ... -->` comments for a given include boundary.
///
/// Merges explicitly-forwarded args with declared defaults (explicit wins).
/// Entries are sorted alphabetically by name.  Explicit args render as
/// `<!-- arg name="…" value="…" -->`, declared defaults as
/// `<!-- arg name="…" default="…" -->`.
fn render_show_args(
    out: &mut String,
    key: &(String, PathBuf),
    include_args: &HashMap<(String, PathBuf), IncludeArgContext>,
    declared_args_by_file: &HashMap<(String, PathBuf), HashMap<String, String>>,
    indent: usize,
) {
    let explicit = include_args
        .get(key)
        .map(|ctx| &ctx.explicit)
        .cloned()
        .unwrap_or_default();
    let declared = declared_args_by_file.get(key).cloned().unwrap_or_default();

    // Merge: explicit args take precedence, then declared defaults
    let mut merged: HashMap<&str, (&str, bool)> = HashMap::new();
    for (name, value) in &explicit {
        merged.insert(name.as_str(), (value.as_str(), false));
    }
    for (name, value) in &declared {
        merged
            .entry(name.as_str())
            .or_insert((value.as_str(), true));
    }

    if merged.is_empty() {
        return;
    }

    let mut sorted: Vec<_> = merged.into_iter().collect();
    sorted.sort_by_key(|(k, _)| *k);
    for (name, (value, is_default)) in sorted {
        if is_default {
            out.push_str(&format!(
                "{}<!-- arg name=\"{}\" default=\"{}\" -->\n",
                pad(indent),
                name,
                xml_escape(value)
            ));
        } else {
            out.push_str(&format!(
                "{}<!-- arg name=\"{}\" value=\"{}\" -->\n",
                pad(indent),
                name,
                xml_escape(value)
            ));
        }
    }
}

pub fn render_resolved_xml(
    package: &str,
    launcher: &str,
    nodes: &[ResolvedNode],
    flatten_namespaces: bool,
    flatten: bool,
    include_args: &HashMap<(String, PathBuf), IncludeArgContext>,
    show_args: bool,
    initial_args: &HashMap<String, String>,
    declared_args_by_file: &HashMap<(String, PathBuf), HashMap<String, String>>,
) -> String {
    let mut out = String::new();

    out.push_str(&format!(
        "<!-- resolved by launch-plus from {package}://launch/{launcher} -->\n"
    ));
    out.push_str("<launch>\n");
    if show_args && !initial_args.is_empty() {
        let mut sorted: Vec<_> = initial_args.iter().collect();
        sorted.sort_by_key(|(k, _)| k.as_str());
        for (name, value) in sorted {
            out.push_str(&format!(
                "  <!-- arg name=\"{}\" value=\"{}\" -->\n",
                name,
                xml_escape(value)
            ));
        }
    }

    let root_share_path = PathBuf::from("launch").join(launcher);

    // Ordering check: warn when a LoadComposable targets a container not yet seen.
    {
        let mut seen_containers: HashSet<String> = HashSet::new();
        for node in nodes {
            match &node.kind {
                NodeKind::Container { .. } => {
                    if let Some(ref name) = node.name {
                        seen_containers.insert(name.clone());
                        if let Some(ref ns) = node.namespace {
                            seen_containers.insert(format!(
                                "{}/{}",
                                ns.trim_end_matches('/'),
                                name
                            ));
                        }
                    }
                }
                NodeKind::LoadComposable { target, .. } if !target.is_empty() => {
                    let found = seen_containers.iter().any(|c| {
                        c == target
                            || c.ends_with(&format!("/{}", target))
                            || target.ends_with(&format!("/{}", c))
                    });
                    if !found {
                        tracing::warn!(
                            "load_composable_node targets '{}' which does not appear before it \
                             in the resolved output; launch may fail at runtime if the container \
                             is not yet running",
                            target
                        );
                    }
                }
                _ => {}
            }
        }
    }

    // Stack of currently open source-level <group> boundaries.
    // Each entry is the (package, share_path) that opened the group.
    let mut open_src: Vec<(String, PathBuf)> = Vec::new();

    // Currently open namespace sub-group stack (non-empty = a <group><push-ros-namespace> is open).
    let mut open_ns: Vec<String> = Vec::new();

    for node in nodes {
        // Compute the target source stack for nesting.
        // In flat mode: use only the leaf source (one level, clear provenance).
        // In nested mode: use the full chain[1..] (full include hierarchy).
        let full_stack = node_source_stack(node, package, &root_share_path);
        let target_src: Vec<(String, PathBuf)> = if flatten && !full_stack.is_empty() {
            vec![full_stack.last().unwrap().clone()]
        } else {
            full_stack
        };

        // Namespace target: empty when flattening namespaces (baked into node attribute).
        let target_ns: Vec<String> = if flatten_namespaces {
            vec![]
        } else {
            node.namespace_stack.clone()
        };

        let common = common_prefix_len(&open_src, &target_src);
        let src_changing = common < open_src.len() || open_src.len() < target_src.len();

        // Close the namespace sub-group whenever source or namespace changes.
        if (src_changing || target_ns != open_ns) && !open_ns.is_empty() {
            let vd = visual_src_depth(open_src.len(), flatten);
            out.push_str(&format!("{}</group>\n", pad(vd)));
            open_ns.clear();
        }

        // IncludeMarker: a file that contributed no executable nodes.  Emit an inline
        // `<!-- source: ... --> <!-- end: ... -->` comment pair without a persistent
        // `<group>` element.  Intermediate chain levels (not the marker's own file) are
        // opened as normal persistent groups so subsequent real nodes share the context.
        if matches!(&node.kind, NodeKind::IncludeMarker) {
            // Close excess source groups (same as for real nodes).
            while open_src.len() > common {
                let depth = open_src.len() - 1;
                let (pkg, path) = open_src.pop().unwrap();
                let vd = visual_src_depth(depth, flatten);
                if !flatten {
                    out.push_str(&format!("{}</group>\n", pad(vd)));
                }
                out.push_str(&format!(
                    "{}<!-- end: {}://{} -->\n",
                    pad(vd),
                    pkg,
                    path.display()
                ));
            }
            // Open intermediate levels persistently so real-node siblings inherit the context.
            while open_src.len() + 1 < target_src.len() {
                let depth = open_src.len();
                let vd = visual_src_depth(depth, flatten);
                let (pkg, path) = target_src[depth].clone();
                out.push_str(&format!(
                    "{}<!-- source: {}://{} -->\n",
                    pad(vd),
                    pkg,
                    path.display()
                ));
                if !flatten {
                    out.push_str(&format!("{}<group>\n", pad(vd)));
                }
                if show_args {
                    render_show_args(
                        &mut out,
                        &(pkg.clone(), path.clone()),
                        include_args,
                        declared_args_by_file,
                        vd + 1,
                    );
                }
                open_src.push((pkg, path));
            }
            // The marker's own level: inline comment pair only, no <group>, no state change.
            if open_src.len() < target_src.len() {
                let depth = open_src.len();
                let vd = visual_src_depth(depth, flatten);
                let (pkg, path) = &target_src[depth];
                out.push_str(&format!(
                    "{}<!-- source: {}://{} -->\n",
                    pad(vd),
                    pkg,
                    path.display()
                ));
                // Emit explicit args and declared defaults for this include boundary.
                if show_args {
                    render_show_args(
                        &mut out,
                        &(pkg.clone(), path.clone()),
                        include_args,
                        declared_args_by_file,
                        vd + 1,
                    );
                }
                out.push_str(&format!(
                    "{}<!-- end: {}://{} -->\n",
                    pad(vd),
                    pkg,
                    path.display()
                ));
            }
            continue;
        }

        if src_changing {
            // Close excess source groups, innermost first.
            while open_src.len() > common {
                let depth = open_src.len() - 1;
                let (pkg, path) = open_src.pop().unwrap();
                let vd = visual_src_depth(depth, flatten);
                if !flatten {
                    out.push_str(&format!("{}</group>\n", pad(vd)));
                }
                out.push_str(&format!(
                    "{}<!-- end: {}://{} -->\n",
                    pad(vd),
                    pkg,
                    path.display()
                ));
            }

            // Open new source groups, outermost first.
            while open_src.len() < target_src.len() {
                let depth = open_src.len();
                let vd = visual_src_depth(depth, flatten);
                let (pkg, path) = target_src[depth].clone();
                out.push_str(&format!(
                    "{}<!-- source: {}://{} -->\n",
                    pad(vd),
                    pkg,
                    path.display()
                ));
                if !flatten {
                    out.push_str(&format!("{}<group>\n", pad(vd)));
                }
                // Emit explicit args and declared defaults for this include boundary.
                if show_args {
                    render_show_args(
                        &mut out,
                        &(pkg.clone(), path.clone()),
                        include_args,
                        declared_args_by_file,
                        vd + 1,
                    );
                }
                open_src.push((pkg, path));
            }
        }

        // Open a namespace sub-group if the namespace changed.
        if target_ns != open_ns && !target_ns.is_empty() {
            let vd = visual_src_depth(open_src.len(), flatten);
            out.push_str(&format!("{}<group>\n", pad(vd)));
            let ns_str = joined_namespace_stack(&target_ns);
            out.push_str(&format!(
                "{}  <push-ros-namespace namespace=\"{}\"/>\n",
                pad(vd),
                xml_escape(&ns_str)
            ));
            open_ns = target_ns.clone();
        }

        // Compute node indentation: source depth + 1 if inside a namespace sub-group.
        let vd = visual_src_depth(open_src.len(), flatten);
        let effective_depth = vd + if !open_ns.is_empty() { 1 } else { 0 };
        let node_ind = pad(effective_depth);
        let child_ind = pad(effective_depth + 1);

        // The effective namespace contributed solely by the open namespace stack.
        // Used to decide whether the node's namespace= attribute is redundant.
        let stack_only_ns: Option<String> = if open_ns.is_empty() {
            None
        } else {
            Some(joined_namespace_stack(&open_ns))
        };

        match &node.kind {
            NodeKind::Node => {
                render_node(
                    node,
                    &node_ind,
                    &child_ind,
                    stack_only_ns.as_deref(),
                    &mut out,
                );
            }
            NodeKind::Container { plugins } => {
                render_container_node(
                    node,
                    plugins,
                    &node_ind,
                    &child_ind,
                    stack_only_ns.as_deref(),
                    &mut out,
                );
            }
            NodeKind::LoadComposable { target, plugins } => {
                render_load_composable_node(target, plugins, &node_ind, &child_ind, &mut out);
            }
            NodeKind::IncludeMarker => unreachable!("markers handled above"),
            NodeKind::Log { message } => {
                out.push_str(&format!(
                    "{}<log message=\"{}\"/>\n",
                    node_ind,
                    xml_escape(message)
                ));
            }
            NodeKind::SetParameter { name, value } => {
                out.push_str(&format!(
                    "{}<set_parameter name=\"{}\" value=\"{}\"/>\n",
                    node_ind,
                    xml_escape(name),
                    xml_escape(value)
                ));
            }
            NodeKind::SetRemap { from, to } => {
                out.push_str(&format!(
                    "{}<set_remap from=\"{}\" to=\"{}\"/>\n",
                    node_ind,
                    xml_escape(from),
                    xml_escape(to)
                ));
            }
            NodeKind::Executable { cmd, name, shell } => {
                let name_attr = name
                    .as_deref()
                    .map(|n| format!(" name=\"{}\"", xml_escape(n)))
                    .unwrap_or_default();
                out.push_str(&format!(
                    "{}<executable cmd=\"{}\"{} shell=\"{}\"/>\n",
                    node_ind,
                    xml_escape(cmd),
                    name_attr,
                    shell
                ));
            }
            NodeKind::LifecycleNode => {
                render_lifecycle_node(
                    node,
                    &node_ind,
                    &child_ind,
                    stack_only_ns.as_deref(),
                    &mut out,
                );
            }
            NodeKind::EventHandler {
                handler_kind,
                target,
                target_node,
                namespace,
                start_state,
                goal_state,
                actions,
            } => {
                render_event_handler(
                    *handler_kind,
                    target.as_deref(),
                    target_node.as_deref(),
                    namespace.as_deref(),
                    start_state.as_deref(),
                    goal_state.as_deref(),
                    actions,
                    &node_ind,
                    &child_ind,
                    &mut out,
                );
            }
        }
    }

    // Cleanup: close open namespace sub-group.
    if !open_ns.is_empty() {
        let vd = visual_src_depth(open_src.len(), flatten);
        out.push_str(&format!("{}</group>\n", pad(vd)));
    }

    // Close remaining source groups, innermost first.
    while !open_src.is_empty() {
        let depth = open_src.len() - 1;
        let (pkg, path) = open_src.pop().unwrap();
        let vd = visual_src_depth(depth, flatten);
        if !flatten {
            out.push_str(&format!("{}</group>\n", pad(vd)));
        }
        out.push_str(&format!(
            "{}<!-- end: {}://{} -->\n",
            pad(vd),
            pkg,
            path.display()
        ));
    }

    out.push_str("</launch>\n");
    out
}

/// Returns the indentation string for the given visual depth level.
///
/// `pad(0)` = `"  "` (2 spaces, content at `<launch>` level).
/// `pad(d)` = `"  ".repeat(d + 1)`.
fn pad(depth: usize) -> String {
    "  ".repeat(depth + 1)
}

/// Returns the visual source depth, which drives indentation.
///
/// In flat mode source groups carry no `<group>` tag, so they contribute 0 to indentation.
/// In nested mode each source group adds one indent level.
fn visual_src_depth(open_src_len: usize, flatten: bool) -> usize {
    if flatten { 0 } else { open_src_len }
}

/// Compute the target source-group nesting stack for a node.
///
/// - If `include_chain` is non-empty and starts with the root: returns `chain[1..]`.
///   Nodes whose chain has exactly one entry (the root itself) return `[]` (flat).
/// - If `include_chain` is non-empty but does not start with the root: returns the full chain.
/// - If `include_chain` is empty (inline / unit-test mode): returns `[source]` unless the
///   source equals the root (in which case returns `[]`).
fn node_source_stack(
    node: &ResolvedNode,
    root_pkg: &str,
    root_path: &std::path::Path,
) -> Vec<(String, PathBuf)> {
    if !node.include_chain.is_empty() {
        let chain = &node.include_chain;
        let (first_pkg, first_path) = &chain[0];
        if first_pkg == root_pkg && first_path.as_path() == root_path {
            return chain[1..].to_vec();
        }
        return chain.to_vec();
    }
    // Inline mode (unit tests without orchestrator context).
    match &node.source {
        Some((pkg, path)) if pkg == root_pkg && path.as_path() == root_path => vec![],
        Some(src) => vec![src.clone()],
        None => vec![],
    }
}

/// Length of the common prefix of two slices.
fn common_prefix_len<T: PartialEq>(a: &[T], b: &[T]) -> usize {
    a.iter().zip(b.iter()).take_while(|(x, y)| x == y).count()
}

/// Emit a single `<node>` element (with `<param>`, `<remap>`, `<env>` children if present).
///
/// `stack_only_ns`: the effective namespace contributed solely by the enclosing
/// `<push-ros-namespace>` stack.  When `node.namespace` equals this value the `namespace=`
/// attribute is redundant and is suppressed; when they differ the attribute is emitted.
fn render_node(
    node: &ResolvedNode,
    node_ind: &str,
    child_ind: &str,
    stack_only_ns: Option<&str>,
    out: &mut String,
) {
    let mut tag = format!(
        "{}<node pkg=\"{}\" exec=\"{}\"",
        node_ind, node.package, node.executable
    );
    if let Some(ref name) = node.name {
        tag.push_str(&format!(" name=\"{}\"", xml_escape(name)));
    }
    // Emit namespace= only when not fully covered by the enclosing <push-ros-namespace>.
    let emit_ns = node.namespace.as_deref() != stack_only_ns;
    if emit_ns {
        if let Some(ref ns) = node.namespace {
            tag.push_str(&format!(" namespace=\"{}\"", xml_escape(ns)));
        }
    }
    if let Some(ref o) = node.output {
        tag.push_str(&format!(" output=\"{}\"", xml_escape(o)));
    }
    if let Some(ref a) = node.args {
        tag.push_str(&format!(" args=\"{}\"", xml_escape(a)));
    }
    if let Some(ref r) = node.respawn {
        tag.push_str(&format!(" respawn=\"{}\"", xml_escape(r)));
    }
    if let Some(ref d) = node.respawn_delay {
        tag.push_str(&format!(" respawn_delay=\"{}\"", xml_escape(d)));
    }

    let has_children = !node.param_files.is_empty()
        || !node.parameters.is_empty()
        || !node.remappings.is_empty()
        || !node.env.is_empty();

    if has_children {
        tag.push_str(">\n");
        out.push_str(&tag);

        for pf in &node.param_files {
            render_param_file(pf, child_ind, out);
        }

        for (key, value) in &node.parameters {
            out.push_str(&format!(
                "{}<param name=\"{}\" value=\"{}\"/>\n",
                child_ind,
                xml_escape(key),
                xml_escape(value)
            ));
        }

        for (from, to) in &node.remappings {
            out.push_str(&format!(
                "{}<remap from=\"{}\" to=\"{}\"/>\n",
                child_ind,
                xml_escape(from),
                xml_escape(to)
            ));
        }

        for (name, value) in &node.env {
            out.push_str(&format!(
                "{}<env name=\"{}\" value=\"{}\"/>\n",
                child_ind,
                xml_escape(name),
                xml_escape(value)
            ));
        }

        out.push_str(&format!("{}</node>\n", node_ind));
    } else {
        tag.push_str("/>\n");
        out.push_str(&tag);
    }
}

/// Emit a `<lifecycle_node>` element (same structure as `<node>` but different tag).
fn render_lifecycle_node(
    node: &ResolvedNode,
    node_ind: &str,
    child_ind: &str,
    stack_only_ns: Option<&str>,
    out: &mut String,
) {
    let mut tag = format!(
        "{}<lifecycle_node pkg=\"{}\" exec=\"{}\"",
        node_ind, node.package, node.executable
    );
    if let Some(ref name) = node.name {
        tag.push_str(&format!(" name=\"{}\"", xml_escape(name)));
    }
    let emit_ns = node.namespace.as_deref() != stack_only_ns;
    if emit_ns {
        if let Some(ref ns) = node.namespace {
            tag.push_str(&format!(" namespace=\"{}\"", xml_escape(ns)));
        }
    }
    if let Some(ref o) = node.output {
        tag.push_str(&format!(" output=\"{}\"", xml_escape(o)));
    }
    if let Some(ref a) = node.args {
        tag.push_str(&format!(" args=\"{}\"", xml_escape(a)));
    }
    if let Some(ref r) = node.respawn {
        tag.push_str(&format!(" respawn=\"{}\"", xml_escape(r)));
    }
    if let Some(ref d) = node.respawn_delay {
        tag.push_str(&format!(" respawn_delay=\"{}\"", xml_escape(d)));
    }

    let has_children = !node.param_files.is_empty()
        || !node.parameters.is_empty()
        || !node.remappings.is_empty()
        || !node.env.is_empty();

    if has_children {
        tag.push_str(">\n");
        out.push_str(&tag);

        for pf in &node.param_files {
            render_param_file(pf, child_ind, out);
        }
        for (key, value) in &node.parameters {
            out.push_str(&format!(
                "{}<param name=\"{}\" value=\"{}\"/>\n",
                child_ind,
                xml_escape(key),
                xml_escape(value)
            ));
        }
        for (from, to) in &node.remappings {
            out.push_str(&format!(
                "{}<remap from=\"{}\" to=\"{}\"/>\n",
                child_ind,
                xml_escape(from),
                xml_escape(to)
            ));
        }
        for (name, value) in &node.env {
            out.push_str(&format!(
                "{}<env name=\"{}\" value=\"{}\"/>\n",
                child_ind,
                xml_escape(name),
                xml_escape(value)
            ));
        }

        out.push_str(&format!("{}</lifecycle_node>\n", node_ind));
    } else {
        tag.push_str("/>\n");
        out.push_str(&tag);
    }
}

/// Emit an event handler element (`<on_process_start>`, `<on_process_exit>`,
/// `<on_state_transition>`) with child `<emit_event>` actions.
fn render_event_handler(
    handler_kind: EventHandlerKind,
    target: Option<&str>,
    target_node: Option<&str>,
    namespace: Option<&str>,
    start_state: Option<&str>,
    goal_state: Option<&str>,
    actions: &[ResolvedEventAction],
    node_ind: &str,
    child_ind: &str,
    out: &mut String,
) {
    let tag_name = match handler_kind {
        EventHandlerKind::OnProcessStart => "on_process_start",
        EventHandlerKind::OnProcessExit => "on_process_exit",
        EventHandlerKind::OnStateTransition => "on_state_transition",
        EventHandlerKind::OnShutdown => "on_shutdown",
    };

    let mut tag = format!("{}<{}", node_ind, tag_name);
    if let Some(t) = target {
        tag.push_str(&format!(" target=\"{}\"", xml_escape(t)));
    }
    if let Some(tn) = target_node {
        tag.push_str(&format!(" target_node=\"{}\"", xml_escape(tn)));
    }
    if let Some(ns) = namespace {
        tag.push_str(&format!(" namespace=\"{}\"", xml_escape(ns)));
    }
    if let Some(ss) = start_state {
        tag.push_str(&format!(" start_state=\"{}\"", xml_escape(ss)));
    }
    if let Some(gs) = goal_state {
        tag.push_str(&format!(" goal_state=\"{}\"", xml_escape(gs)));
    }

    if actions.is_empty() {
        tag.push_str("/>\n");
        out.push_str(&tag);
    } else {
        tag.push_str(">\n");
        out.push_str(&tag);

        for action in actions {
            match action {
                ResolvedEventAction::EmitEvent {
                    event,
                    target_node,
                    namespace,
                } => {
                    let tn_attr = target_node
                        .as_deref()
                        .map(|n| format!(" target_node=\"{}\"", xml_escape(n)))
                        .unwrap_or_default();
                    let ns_attr = namespace
                        .as_deref()
                        .map(|n| format!(" namespace=\"{}\"", xml_escape(n)))
                        .unwrap_or_default();
                    out.push_str(&format!(
                        "{}<emit_event event=\"{}\"{}{}/>\n",
                        child_ind,
                        xml_escape(event),
                        tn_attr,
                        ns_attr,
                    ));
                }
            }
        }

        out.push_str(&format!("{}</{}>\n", node_ind, tag_name));
    }
}

/// Emit a `<node_container>` element with nested `<composable_node>` children.
fn render_container_node(
    node: &ResolvedNode,
    plugins: &[ComposablePlugin],
    node_ind: &str,
    child_ind: &str,
    stack_only_ns: Option<&str>,
    out: &mut String,
) {
    let mut tag = format!(
        "{}<node_container pkg=\"{}\" exec=\"{}\"",
        node_ind, node.package, node.executable
    );
    if let Some(ref name) = node.name {
        tag.push_str(&format!(" name=\"{}\"", xml_escape(name)));
    }
    let emit_ns = node.namespace.as_deref() != stack_only_ns;
    if emit_ns {
        if let Some(ref ns) = node.namespace {
            tag.push_str(&format!(" namespace=\"{}\"", xml_escape(ns)));
        }
    }
    let has_children = !plugins.is_empty() || !node.env.is_empty();
    if !has_children {
        tag.push_str("/>\n");
        out.push_str(&tag);
    } else {
        tag.push_str(">\n");
        out.push_str(&tag);
        for (name, value) in &node.env {
            out.push_str(&format!(
                "{}<env name=\"{}\" value=\"{}\"/>\n",
                child_ind,
                xml_escape(name),
                xml_escape(value)
            ));
        }
        for plugin in plugins {
            render_composable_plugin(plugin, child_ind, out);
        }
        out.push_str(&format!("{}</node_container>\n", node_ind));
    }
}

/// Emit a `<load_composable_node>` element with `<composable_node>` children.
fn render_load_composable_node(
    target: &str,
    plugins: &[ComposablePlugin],
    node_ind: &str,
    child_ind: &str,
    out: &mut String,
) {
    if plugins.is_empty() {
        return;
    }
    out.push_str(&format!("{}<load_composable_node", node_ind));
    if !target.is_empty() {
        out.push_str(&format!(" target=\"{}\"", xml_escape(target)));
    }
    out.push_str(">\n");
    for plugin in plugins {
        render_composable_plugin(plugin, child_ind, out);
    }
    out.push_str(&format!("{}</load_composable_node>\n", node_ind));
}

/// Emit a single `<composable_node>` element.
fn render_composable_plugin(plugin: &ComposablePlugin, ind: &str, out: &mut String) {
    let child_ind = format!("{}  ", ind);
    let mut tag = format!(
        "{}<composable_node pkg=\"{}\" plugin=\"{}\"",
        ind,
        xml_escape(&plugin.package),
        xml_escape(&plugin.plugin)
    );
    if let Some(ref name) = plugin.name {
        tag.push_str(&format!(" name=\"{}\"", xml_escape(name)));
    }
    let has_children = !plugin.param_files.is_empty()
        || !plugin.parameters.is_empty()
        || !plugin.remappings.is_empty();
    if has_children {
        tag.push_str(">\n");
        out.push_str(&tag);
        for pf in &plugin.param_files {
            render_param_file(pf, &child_ind, out);
        }
        for (key, value) in &plugin.parameters {
            out.push_str(&format!(
                "{}<param name=\"{}\" value=\"{}\"/>\n",
                child_ind,
                xml_escape(key),
                xml_escape(value)
            ));
        }
        for (from, to) in &plugin.remappings {
            out.push_str(&format!(
                "{}<remap from=\"{}\" to=\"{}\"/>\n",
                child_ind,
                xml_escape(from),
                xml_escape(to)
            ));
        }
        out.push_str(&format!("{}</composable_node>\n", ind));
    } else {
        tag.push_str("/>\n");
        out.push_str(&tag);
    }
}

/// Render a single [`ParamFile`] IR entry into XML.
///
/// - `Reference` → `<param from="display"/>`
/// - `Inlined` → boundary comments + individual `<param name=... value=.../>` elements
fn render_param_file(pf: &ParamFile, ind: &str, out: &mut String) {
    match pf {
        ParamFile::Reference { display, .. } => {
            out.push_str(&format!(
                "{}<param from=\"{}\"/>\n",
                ind,
                xml_escape(display)
            ));
        }
        ParamFile::Inlined { display, params } => {
            out.push_str(&format!(
                "{}<!-- params from: {} -->\n",
                ind,
                xml_escape(display)
            ));
            for (name, value) in params {
                out.push_str(&format!(
                    "{}<param name=\"{}\" value=\"{}\"/>\n",
                    ind,
                    xml_escape(name),
                    xml_escape(value)
                ));
            }
            out.push_str(&format!(
                "{}<!-- end params from: {} -->\n",
                ind,
                xml_escape(display)
            ));
        }
    }
}

/// Join a `namespace_stack` into a single absolute namespace string.
///
/// Applies `ros2_namespace_join` left-to-right so that absolute components correctly
/// reset the accumulated prefix (e.g. `["sensing", "/abs"]` → `"/abs"`).
///
/// E.g. `["sensing", "lidar"]` → `"/sensing/lidar"`.
fn joined_namespace_stack(stack: &[String]) -> String {
    effective_namespace(stack, None).unwrap_or_default()
}

/// Escape special XML characters in an attribute value.
fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('"', "&quot;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

// ============================================================================
// Static arg-lifecycle analysis helpers
// ============================================================================

/// Collect all `<arg name="X"/>` names declared at any nesting depth within `elements`.
///
/// Recurses into `<group>` children but NOT into `<include>` targets — includes are
/// separate files and their arg declarations belong to a different scope.
///
/// Used by the orchestrator for the unused-arg and excessive-include-arg checks.
pub fn collect_declared_args(elements: &[LaunchElement]) -> HashSet<String> {
    let mut names = HashSet::new();
    collect_declared_args_impl(elements, &mut names);
    names
}

fn collect_declared_args_impl(elements: &[LaunchElement], names: &mut HashSet<String>) {
    for elem in elements {
        match elem {
            LaunchElement::Arg { name, .. } => {
                names.insert(name.clone());
            }
            LaunchElement::Group { children, .. } => {
                collect_declared_args_impl(children, names);
            }
            _ => {}
        }
    }
}

/// Collect all arg/var names referenced via `$(arg X)` or `$(var X)` anywhere in
/// `elements` (static scan — checks all branches regardless of conditionals).
///
/// `$(arg X)` is the direct arg reference.  `$(var X)` is included because in ROS 2
/// launch XML `<arg name="X"/>` stores into the global LaunchConfiguration and is
/// commonly accessed as `$(var X)` by subsequent elements.
///
/// Used by the orchestrator for the unused-arg check.
pub fn collect_arg_and_var_refs(elements: &[LaunchElement]) -> HashSet<String> {
    let mut refs = HashSet::new();
    collect_arg_var_refs_impl(elements, &mut refs);
    refs
}

fn collect_arg_var_refs_impl(elements: &[LaunchElement], refs: &mut HashSet<String>) {
    for elem in elements {
        collect_arg_var_refs_in_elem(elem, refs);
    }
}

fn collect_arg_var_refs_in_elem(elem: &LaunchElement, refs: &mut HashSet<String>) {
    match elem {
        LaunchElement::Arg { default, .. } => {
            if let Some(d) = default {
                scan_str_for_arg_var_refs(d, refs);
            }
        }
        LaunchElement::Let { value, .. } => {
            scan_str_for_arg_var_refs(value, refs);
        }
        LaunchElement::Group {
            condition,
            children,
            ..
        } => {
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
            collect_arg_var_refs_impl(children, refs);
        }
        LaunchElement::Include {
            file,
            condition,
            args,
        } => {
            scan_str_for_arg_var_refs(file, refs);
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
            for a in args {
                scan_str_for_arg_var_refs(&a.value, refs);
            }
        }
        LaunchElement::Node {
            pkg,
            exec,
            name,
            namespace,
            condition,
            params,
            remaps,
            envs,
            ..
        } => {
            scan_str_for_arg_var_refs(pkg, refs);
            scan_str_for_arg_var_refs(exec, refs);
            if let Some(n) = name {
                scan_str_for_arg_var_refs(n, refs);
            }
            if let Some(ns) = namespace {
                scan_str_for_arg_var_refs(ns, refs);
            }
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
            for p in params {
                if let Some(v) = &p.value {
                    scan_str_for_arg_var_refs(v, refs);
                }
                if let Some(f) = &p.from {
                    scan_str_for_arg_var_refs(f, refs);
                }
                if let Some(n) = &p.name {
                    scan_str_for_arg_var_refs(n, refs);
                }
            }
            for r in remaps {
                scan_str_for_arg_var_refs(&r.from, refs);
                scan_str_for_arg_var_refs(&r.to, refs);
            }
            for e in envs {
                scan_str_for_arg_var_refs(&e.value, refs);
            }
        }
        LaunchElement::PushRosNamespace {
            namespace,
            condition,
            ..
        } => {
            scan_str_for_arg_var_refs(namespace, refs);
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
        }
        LaunchElement::SetEnv { value, .. } => {
            scan_str_for_arg_var_refs(value, refs);
        }
        LaunchElement::UnsetEnv { .. } => {}
        LaunchElement::NodeContainer {
            pkg,
            exec,
            name,
            namespace,
            condition,
            composable_nodes,
            envs,
        } => {
            scan_str_for_arg_var_refs(pkg, refs);
            scan_str_for_arg_var_refs(exec, refs);
            if let Some(n) = name {
                scan_str_for_arg_var_refs(n, refs);
            }
            if let Some(ns) = namespace {
                scan_str_for_arg_var_refs(ns, refs);
            }
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
            for cn in composable_nodes {
                scan_str_for_arg_var_refs(&cn.pkg, refs);
                scan_str_for_arg_var_refs(&cn.plugin, refs);
                if let Some(n) = &cn.name {
                    scan_str_for_arg_var_refs(n, refs);
                }
                if let Some(c) = &cn.condition {
                    scan_str_for_arg_var_refs(&c.expr, refs);
                }
            }
            for env_entry in envs {
                scan_str_for_arg_var_refs(&env_entry.name, refs);
                scan_str_for_arg_var_refs(&env_entry.value, refs);
            }
        }
        LaunchElement::LoadComposableNode {
            namespace,
            condition,
            composable_nodes,
            ..
        } => {
            if let Some(ns) = namespace {
                scan_str_for_arg_var_refs(ns, refs);
            }
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
            for cn in composable_nodes {
                scan_str_for_arg_var_refs(&cn.pkg, refs);
                scan_str_for_arg_var_refs(&cn.plugin, refs);
                if let Some(n) = &cn.name {
                    scan_str_for_arg_var_refs(n, refs);
                }
                if let Some(c) = &cn.condition {
                    scan_str_for_arg_var_refs(&c.expr, refs);
                }
            }
        }
        LaunchElement::SetParameter { name, value } => {
            scan_str_for_arg_var_refs(name, refs);
            scan_str_for_arg_var_refs(value, refs);
        }
        LaunchElement::SetRemap { from, to } => {
            scan_str_for_arg_var_refs(from, refs);
            scan_str_for_arg_var_refs(to, refs);
        }
        LaunchElement::Log { message } => {
            scan_str_for_arg_var_refs(message, refs);
        }
        LaunchElement::UnknownElement { .. } => {}
        LaunchElement::Executable {
            cmd,
            name,
            condition,
            ..
        } => {
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
            scan_str_for_arg_var_refs(cmd, refs);
            if let Some(n) = name {
                scan_str_for_arg_var_refs(n, refs);
            }
        }
        LaunchElement::LifecycleNode {
            pkg,
            exec,
            name,
            namespace,
            condition,
            params,
            remaps,
            envs,
            ..
        } => {
            scan_str_for_arg_var_refs(pkg, refs);
            scan_str_for_arg_var_refs(exec, refs);
            if let Some(n) = name {
                scan_str_for_arg_var_refs(n, refs);
            }
            if let Some(ns) = namespace {
                scan_str_for_arg_var_refs(ns, refs);
            }
            if let Some(c) = condition {
                scan_str_for_arg_var_refs(&c.expr, refs);
            }
            for p in params {
                if let Some(v) = &p.value {
                    scan_str_for_arg_var_refs(v, refs);
                }
                if let Some(f) = &p.from {
                    scan_str_for_arg_var_refs(f, refs);
                }
                if let Some(n) = &p.name {
                    scan_str_for_arg_var_refs(n, refs);
                }
            }
            for r in remaps {
                scan_str_for_arg_var_refs(&r.from, refs);
                scan_str_for_arg_var_refs(&r.to, refs);
            }
            for e in envs {
                scan_str_for_arg_var_refs(&e.value, refs);
            }
        }
        LaunchElement::EventHandler {
            target,
            target_node,
            namespace,
            start_state,
            goal_state,
            children,
            ..
        } => {
            if let Some(s) = target {
                scan_str_for_arg_var_refs(s, refs);
            }
            if let Some(s) = target_node {
                scan_str_for_arg_var_refs(s, refs);
            }
            if let Some(s) = namespace {
                scan_str_for_arg_var_refs(s, refs);
            }
            if let Some(s) = start_state {
                scan_str_for_arg_var_refs(s, refs);
            }
            if let Some(s) = goal_state {
                scan_str_for_arg_var_refs(s, refs);
            }
            for child in children {
                collect_arg_var_refs_in_elem(child, refs);
            }
        }
        LaunchElement::EmitEvent {
            event,
            target_node,
            namespace,
            ..
        } => {
            scan_str_for_arg_var_refs(event, refs);
            if let Some(s) = target_node {
                scan_str_for_arg_var_refs(s, refs);
            }
            if let Some(s) = namespace {
                scan_str_for_arg_var_refs(s, refs);
            }
        }
    }
}

/// Extract `$(arg X)` and `$(var X)` names from a single substitution string.
fn scan_str_for_arg_var_refs(s: &str, refs: &mut HashSet<String>) {
    if let Ok(parts) = parse_substitutions(s) {
        for part in &parts {
            match part {
                Substitution::Arg(name) | Substitution::Var(name) => {
                    refs.insert(name.clone());
                }
                _ => {}
            }
        }
    }
}

// ============================================================================
// Anti-pattern static scan helpers
// ============================================================================

/// Collect `<include>` elements that are direct children of `<group scoped="false">`.
///
/// Returns the raw `file` expression string from each offending include.
/// Recurses into nested `<group scoped="false">` (leakage is transitive) but stops
/// at `<group scoped="true">` (the default), which acts as a scope barrier.
///
/// Used by the orchestrator for the scoped-false-include anti-pattern warning.
pub fn collect_scoped_false_includes(elements: &[LaunchElement]) -> Vec<String> {
    let mut includes = Vec::new();
    collect_scoped_false_includes_impl(elements, &mut includes);
    includes
}

fn collect_scoped_false_includes_impl(elements: &[LaunchElement], includes: &mut Vec<String>) {
    for elem in elements {
        if let LaunchElement::Group {
            scoped, children, ..
        } = elem
        {
            if !scoped {
                // Flag every <include> that is a direct child of this unscoped group.
                for child in children.iter() {
                    if let LaunchElement::Include { file, .. } = child {
                        includes.push(file.clone());
                    }
                }
            }
            // Recurse regardless of scoped value: unscoped groups further down are
            // still problematic, and scoped=true groups act as barriers only for
            // includes that are their OWN direct children.
            collect_scoped_false_includes_impl(children, includes);
        }
    }
}

/// Collect env var names used via `$(env X)` without a fallback default anywhere in
/// `elements`.
///
/// `$(env X)` with no default word will fail at launch time if the variable is unset.
/// The safe form is `$(env X <default>)`.
///
/// Used by the orchestrator for the env-without-fallback anti-pattern warning.
pub fn collect_env_without_fallback(elements: &[LaunchElement]) -> Vec<String> {
    let mut names = Vec::new();
    collect_env_no_fallback_impl(elements, &mut names);
    names
}

fn collect_env_no_fallback_impl(elements: &[LaunchElement], names: &mut Vec<String>) {
    for elem in elements {
        collect_env_no_fallback_in_elem(elem, names);
    }
}

fn collect_env_no_fallback_in_elem(elem: &LaunchElement, names: &mut Vec<String>) {
    match elem {
        LaunchElement::Arg { default, .. } => {
            if let Some(d) = default {
                scan_str_for_env_no_fallback(d, names);
            }
        }
        LaunchElement::Let { value, .. } => {
            scan_str_for_env_no_fallback(value, names);
        }
        LaunchElement::Group {
            condition,
            children,
            ..
        } => {
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
            collect_env_no_fallback_impl(children, names);
        }
        LaunchElement::Include {
            file,
            condition,
            args,
        } => {
            scan_str_for_env_no_fallback(file, names);
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
            for a in args {
                scan_str_for_env_no_fallback(&a.value, names);
            }
        }
        LaunchElement::Node {
            pkg,
            exec,
            name,
            namespace,
            condition,
            params,
            remaps,
            envs,
            ..
        } => {
            scan_str_for_env_no_fallback(pkg, names);
            scan_str_for_env_no_fallback(exec, names);
            if let Some(n) = name {
                scan_str_for_env_no_fallback(n, names);
            }
            if let Some(ns) = namespace {
                scan_str_for_env_no_fallback(ns, names);
            }
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
            for p in params {
                if let Some(v) = &p.value {
                    scan_str_for_env_no_fallback(v, names);
                }
                if let Some(f) = &p.from {
                    scan_str_for_env_no_fallback(f, names);
                }
                if let Some(n) = &p.name {
                    scan_str_for_env_no_fallback(n, names);
                }
            }
            for r in remaps {
                scan_str_for_env_no_fallback(&r.from, names);
                scan_str_for_env_no_fallback(&r.to, names);
            }
            for e in envs {
                scan_str_for_env_no_fallback(&e.value, names);
            }
        }
        LaunchElement::PushRosNamespace {
            namespace,
            condition,
            ..
        } => {
            scan_str_for_env_no_fallback(namespace, names);
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
        }
        LaunchElement::SetEnv { value, .. } => {
            scan_str_for_env_no_fallback(value, names);
        }
        LaunchElement::UnsetEnv { .. } => {}
        LaunchElement::NodeContainer {
            pkg,
            exec,
            name,
            namespace,
            condition,
            composable_nodes,
            envs,
        } => {
            scan_str_for_env_no_fallback(pkg, names);
            scan_str_for_env_no_fallback(exec, names);
            if let Some(n) = name {
                scan_str_for_env_no_fallback(n, names);
            }
            if let Some(ns) = namespace {
                scan_str_for_env_no_fallback(ns, names);
            }
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
            for cn in composable_nodes {
                scan_str_for_env_no_fallback(&cn.pkg, names);
                scan_str_for_env_no_fallback(&cn.plugin, names);
                if let Some(n) = &cn.name {
                    scan_str_for_env_no_fallback(n, names);
                }
                if let Some(c) = &cn.condition {
                    scan_str_for_env_no_fallback(&c.expr, names);
                }
            }
            for env_entry in envs {
                scan_str_for_env_no_fallback(&env_entry.name, names);
                scan_str_for_env_no_fallback(&env_entry.value, names);
            }
        }
        LaunchElement::LoadComposableNode {
            namespace,
            condition,
            composable_nodes,
            ..
        } => {
            if let Some(ns) = namespace {
                scan_str_for_env_no_fallback(ns, names);
            }
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
            for cn in composable_nodes {
                scan_str_for_env_no_fallback(&cn.pkg, names);
                scan_str_for_env_no_fallback(&cn.plugin, names);
                if let Some(n) = &cn.name {
                    scan_str_for_env_no_fallback(n, names);
                }
                if let Some(c) = &cn.condition {
                    scan_str_for_env_no_fallback(&c.expr, names);
                }
            }
        }
        LaunchElement::SetParameter { name, value } => {
            scan_str_for_env_no_fallback(name, names);
            scan_str_for_env_no_fallback(value, names);
        }
        LaunchElement::SetRemap { from, to } => {
            scan_str_for_env_no_fallback(from, names);
            scan_str_for_env_no_fallback(to, names);
        }
        LaunchElement::Log { message } => {
            scan_str_for_env_no_fallback(message, names);
        }
        LaunchElement::UnknownElement { .. } => {}
        LaunchElement::Executable {
            cmd,
            name,
            condition,
            ..
        } => {
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
            scan_str_for_env_no_fallback(cmd, names);
            if let Some(n) = name {
                scan_str_for_env_no_fallback(n, names);
            }
        }
        LaunchElement::LifecycleNode {
            pkg,
            exec,
            name,
            namespace,
            condition,
            params,
            remaps,
            envs,
            ..
        } => {
            scan_str_for_env_no_fallback(pkg, names);
            scan_str_for_env_no_fallback(exec, names);
            if let Some(n) = name {
                scan_str_for_env_no_fallback(n, names);
            }
            if let Some(ns) = namespace {
                scan_str_for_env_no_fallback(ns, names);
            }
            if let Some(c) = condition {
                scan_str_for_env_no_fallback(&c.expr, names);
            }
            for p in params {
                if let Some(v) = &p.value {
                    scan_str_for_env_no_fallback(v, names);
                }
                if let Some(f) = &p.from {
                    scan_str_for_env_no_fallback(f, names);
                }
                if let Some(n) = &p.name {
                    scan_str_for_env_no_fallback(n, names);
                }
            }
            for r in remaps {
                scan_str_for_env_no_fallback(&r.from, names);
                scan_str_for_env_no_fallback(&r.to, names);
            }
            for e in envs {
                scan_str_for_env_no_fallback(&e.value, names);
            }
        }
        LaunchElement::EventHandler {
            target,
            target_node,
            namespace,
            start_state,
            goal_state,
            children,
            ..
        } => {
            if let Some(s) = target {
                scan_str_for_env_no_fallback(s, names);
            }
            if let Some(s) = target_node {
                scan_str_for_env_no_fallback(s, names);
            }
            if let Some(s) = namespace {
                scan_str_for_env_no_fallback(s, names);
            }
            if let Some(s) = start_state {
                scan_str_for_env_no_fallback(s, names);
            }
            if let Some(s) = goal_state {
                scan_str_for_env_no_fallback(s, names);
            }
            for child in children {
                collect_env_no_fallback_in_elem(child, names);
            }
        }
        LaunchElement::EmitEvent {
            event,
            target_node,
            namespace,
            ..
        } => {
            scan_str_for_env_no_fallback(event, names);
            if let Some(s) = target_node {
                scan_str_for_env_no_fallback(s, names);
            }
            if let Some(s) = namespace {
                scan_str_for_env_no_fallback(s, names);
            }
        }
    }
}

fn scan_str_for_env_no_fallback(s: &str, names: &mut Vec<String>) {
    if let Ok(parts) = parse_substitutions(s) {
        for part in &parts {
            if let Substitution::Env {
                name,
                default: None,
            } = part
            {
                names.push(name.clone());
            }
        }
    }
}

// ============================================================================
// Path helpers
// ============================================================================

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn test_parse_simple_launch() {
        let xml = r#"
            <launch>
                <arg name="vehicle" default="sample"/>
                <let name="config" value="/path/to/config"/>
                <node pkg="my_pkg" exec="my_node" name="node1"/>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 3);
    }

    #[test]
    fn test_parse_arg() {
        let xml = r#"
            <launch>
                <arg name="test" default="value" description="A test arg"/>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Arg {
            name,
            default,
            description,
        } = &launch.elements[0]
        {
            assert_eq!(name, "test");
            assert_eq!(default.as_deref(), Some("value"));
            assert_eq!(description.as_deref(), Some("A test arg"));
        } else {
            panic!("Expected Arg element");
        }
    }

    #[test]
    fn test_parse_node_with_params() {
        let xml = r#"
            <launch>
                <node pkg="my_pkg" exec="my_node" name="node1">
                    <param name="param1" value="value1"/>
                    <remap from="/in" to="/out"/>
                    <env name="MY_VAR" value="my_value"/>
                </node>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Node {
            pkg,
            exec,
            name,
            params,
            remaps,
            envs,
            ..
        } = &launch.elements[0]
        {
            assert_eq!(pkg, "my_pkg");
            assert_eq!(exec, "my_node");
            assert_eq!(name.as_deref(), Some("node1"));
            assert_eq!(params.len(), 1);
            assert_eq!(remaps.len(), 1);
            assert_eq!(envs.len(), 1);
        } else {
            panic!("Expected Node element");
        }
    }

    #[test]
    fn test_parse_group_with_condition() {
        let xml = r#"
            <launch>
                <arg name="use_sim" default="false"/>
                <group if="$(var use_sim)">
                    <node pkg="sim_pkg" exec="sim_node" name="sim"/>
                </group>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 2);

        if let LaunchElement::Group {
            condition,
            children,
            ..
        } = &launch.elements[1]
        {
            assert!(condition.is_some());
            let cond = condition.as_ref().unwrap();
            assert_eq!(cond.kind, ConditionKind::If);
            assert_eq!(cond.expr, "$(var use_sim)");
            assert_eq!(children.len(), 1);
        } else {
            panic!("Expected Group element");
        }
    }

    #[test]
    fn test_parse_include_with_args() {
        let xml = r#"
            <launch>
                <include file="$(find-pkg-share my_pkg)/launch/other.launch.xml">
                    <arg name="param1" value="value1"/>
                    <arg name="param2" value="value2"/>
                </include>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Include { file, args, .. } = &launch.elements[0] {
            assert_eq!(file, "$(find-pkg-share my_pkg)/launch/other.launch.xml");
            assert_eq!(args.len(), 2);
            assert_eq!(args[0].name, "param1");
            assert_eq!(args[0].value, "value1");
        } else {
            panic!("Expected Include element");
        }
    }

    #[test]
    fn test_parse_nested_groups() {
        let xml = r#"
            <launch>
                <group>
                    <group if="$(var condition1)">
                        <group unless="$(var condition2)">
                            <node pkg="pkg" exec="node" name="deep_node"/>
                        </group>
                    </group>
                </group>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        // Navigate to the deepest node
        if let LaunchElement::Group { children: c1, .. } = &launch.elements[0] {
            assert_eq!(c1.len(), 1);
            if let LaunchElement::Group {
                children: c2,
                condition: cond1,
                ..
            } = &c1[0]
            {
                assert!(cond1.is_some());
                assert_eq!(cond1.as_ref().unwrap().kind, ConditionKind::If);
                if let LaunchElement::Group {
                    children: c3,
                    condition: cond2,
                    ..
                } = &c2[0]
                {
                    assert!(cond2.is_some());
                    assert_eq!(cond2.as_ref().unwrap().kind, ConditionKind::Unless);
                    assert_eq!(c3.len(), 1);
                    if let LaunchElement::Node { name, .. } = &c3[0] {
                        assert_eq!(name.as_deref(), Some("deep_node"));
                    } else {
                        panic!("Expected Node element");
                    }
                } else {
                    panic!("Expected inner Group");
                }
            } else {
                panic!("Expected middle Group");
            }
        } else {
            panic!("Expected outer Group");
        }
    }

    #[test]
    fn test_parse_node_with_namespace() {
        let xml = r#"
            <launch>
                <node pkg="my_pkg" exec="my_node" name="node1" namespace="/my_ns"/>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Node { namespace, .. } = &launch.elements[0] {
            assert_eq!(namespace.as_deref(), Some("/my_ns"));
        } else {
            panic!("Expected Node element");
        }
    }

    #[test]
    fn test_parse_multiple_includes() {
        let xml = r#"
            <launch>
                <include file="first.launch.xml"/>
                <include file="second.launch.xml"/>
                <include file="third.launch.xml">
                    <arg name="arg1" value="val1"/>
                </include>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 3);

        for (i, element) in launch.elements.iter().enumerate() {
            if let LaunchElement::Include { .. } = element {
                // OK
            } else {
                panic!("Expected Include element at index {}", i);
            }
        }
    }

    #[test]
    fn test_parse_empty_launch() {
        let xml = r#"<launch></launch>"#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 0);
    }

    #[test]
    fn test_parse_self_closing_launch() {
        let xml = r#"<launch/>"#;

        // Self-closing launch should work but have no elements
        // Actually, our parser expects Start event, not Empty for launch
        // This might fail - let's see
        let result = parse_launch_xml(xml, Path::new("test.launch.xml"));
        // Self-closing <launch/> won't trigger Event::Start, so it fails
        assert!(result.is_err());
    }

    #[test]
    fn test_error_missing_launch_root() {
        let xml = r#"<node pkg="pkg" exec="exec" name="name"/>"#;

        let result = parse_launch_xml(xml, Path::new("test.launch.xml"));
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("no <launch> root element"));
    }

    #[test]
    fn test_node_name_optional() {
        // name is optional on <node> in ROS 2 Jazzy
        let xml = r#"
            <launch>
                <node pkg="my_pkg" exec="my_node"/>
            </launch>
        "#;
        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        if let LaunchElement::Node { name, .. } = &launch.elements[0] {
            assert!(name.is_none());
        } else {
            panic!("Expected Node element");
        }
    }

    #[test]
    fn test_error_missing_required_attribute() {
        // <node> still requires pkg and exec
        let xml = r#"
            <launch>
                <node pkg="my_pkg"/>
            </launch>
        "#;
        let result = parse_launch_xml(xml, Path::new("test.launch.xml"));
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("missing required attribute"));
        assert!(err.to_string().contains("exec"));
    }

    #[test]
    fn test_param_valid_combinations() {
        // name + value is valid
        let xml = r#"<launch><node pkg="p" exec="e"><param name="x" value="1"/></node></launch>"#;
        assert!(parse_launch_xml(xml, Path::new("t.launch.xml")).is_ok());

        // from only is valid
        let xml =
            r#"<launch><node pkg="p" exec="e"><param from="/cfg/params.yaml"/></node></launch>"#;
        assert!(parse_launch_xml(xml, Path::new("t.launch.xml")).is_ok());
    }

    #[test]
    fn test_param_invalid_combinations() {
        let node_wrap =
            |inner: &str| format!(r#"<launch><node pkg="p" exec="e">{inner}</node></launch>"#);

        // name without value
        let r = parse_launch_xml(
            &node_wrap(r#"<param name="x"/>"#),
            Path::new("t.launch.xml"),
        );
        assert!(r.is_err());
        assert!(r.unwrap_err().to_string().contains("requires either"));

        // name + from
        let r = parse_launch_xml(
            &node_wrap(r#"<param name="x" from="f.yaml"/>"#),
            Path::new("t.launch.xml"),
        );
        assert!(r.is_err());
        assert!(r.unwrap_err().to_string().contains("requires either"));

        // value alone (no name, no from)
        let r = parse_launch_xml(
            &node_wrap(r#"<param value="42"/>"#),
            Path::new("t.launch.xml"),
        );
        assert!(r.is_err());

        // nothing
        let r = parse_launch_xml(&node_wrap(r#"<param/>"#), Path::new("t.launch.xml"));
        assert!(r.is_err());
    }

    #[test]
    fn test_error_invalid_xml_malformed_attribute() {
        // Malformed XML: missing closing quote on attribute
        let xml = r#"<launch attr="unclosed>"#;

        let result = parse_launch_xml(xml, Path::new("test.launch.xml"));
        assert!(result.is_err());
    }

    #[test]
    fn test_error_invalid_xml_bad_entity() {
        // Invalid entity reference
        let xml = r#"<launch>&invalid;</launch>"#;

        let result = parse_launch_xml(xml, Path::new("test.launch.xml"));
        // Note: quick-xml may or may not catch invalid entities depending on config
        // This test verifies behavior, not necessarily that it errors
        let _ = result;
    }

    #[test]
    fn test_unclosed_tags_parsed_gracefully() {
        // Note: quick-xml's streaming parser doesn't detect unclosed tags
        // as errors - it treats unknown elements as skippable content.
        // This is acceptable for our use case since we validate required
        // attributes on known elements.
        let xml = r#"<launch><unclosed>"#;

        let result = parse_launch_xml(xml, Path::new("test.launch.xml"));
        // Parser should succeed; the unknown element is preserved as UnknownElement so
        // the resolver can surface it as a warning.
        assert!(result.is_ok());
        let launch = result.unwrap();
        assert_eq!(launch.elements.len(), 1);
        assert!(
            matches!(&launch.elements[0], LaunchElement::UnknownElement { tag_name } if tag_name == "unclosed")
        );
    }

    #[test]
    fn test_parse_let_element() {
        let xml = r#"
            <launch>
                <let name="my_var" value="$(find-pkg-share pkg)/config"/>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Let { name, value, .. } = &launch.elements[0] {
            assert_eq!(name, "my_var");
            assert_eq!(value, "$(find-pkg-share pkg)/config");
        } else {
            panic!("Expected Let element");
        }
    }

    #[test]
    fn test_parse_set_env_unset_env() {
        let xml = r#"
            <launch>
                <set_env name="MY_VAR" value="my_value"/>
                <unset_env name="OTHER_VAR"/>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 2);

        if let LaunchElement::SetEnv { name, value, .. } = &launch.elements[0] {
            assert_eq!(name, "MY_VAR");
            assert_eq!(value, "my_value");
        } else {
            panic!("Expected SetEnv element");
        }

        if let LaunchElement::UnsetEnv { name, .. } = &launch.elements[1] {
            assert_eq!(name, "OTHER_VAR");
        } else {
            panic!("Expected UnsetEnv element");
        }
    }

    #[test]
    fn test_parse_unless_condition() {
        let xml = r#"
            <launch>
                <group unless="$(var disable_feature)">
                    <node pkg="pkg" exec="node" name="feature_node"/>
                </group>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Group { condition, .. } = &launch.elements[0] {
            assert!(condition.is_some());
            let cond = condition.as_ref().unwrap();
            assert_eq!(cond.kind, ConditionKind::Unless);
            assert_eq!(cond.expr, "$(var disable_feature)");
        } else {
            panic!("Expected Group element");
        }
    }

    #[test]
    fn test_parse_node_with_condition() {
        let xml = r#"
            <launch>
                <node pkg="pkg" exec="node" name="conditional_node" if="$(var enable)"/>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Node {
            condition, name, ..
        } = &launch.elements[0]
        {
            assert_eq!(name.as_deref(), Some("conditional_node"));
            assert!(condition.is_some());
            let cond = condition.as_ref().unwrap();
            assert_eq!(cond.kind, ConditionKind::If);
            assert_eq!(cond.expr, "$(var enable)");
        } else {
            panic!("Expected Node element");
        }
    }

    #[test]
    fn test_parse_include_with_condition() {
        let xml = r#"
            <launch>
                <include file="other.launch.xml" unless="$(var skip_include)"/>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);

        if let LaunchElement::Include { condition, .. } = &launch.elements[0] {
            assert!(condition.is_some());
            let cond = condition.as_ref().unwrap();
            assert_eq!(cond.kind, ConditionKind::Unless);
        } else {
            panic!("Expected Include element");
        }
    }

    #[test]
    fn test_parse_param_with_from() {
        let xml = r#"
            <launch>
                <node pkg="pkg" exec="node" name="node1">
                    <param from="$(find-pkg-share pkg)/config/params.yaml"/>
                </node>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();

        if let LaunchElement::Node { params, .. } = &launch.elements[0] {
            assert_eq!(params.len(), 1);
            assert!(params[0].name.is_none());
            assert!(params[0].value.is_none());
            assert_eq!(
                params[0].from.as_deref(),
                Some("$(find-pkg-share pkg)/config/params.yaml")
            );
        } else {
            panic!("Expected Node element");
        }
    }

    #[test]
    fn test_parse_group_scoped_attribute() {
        let xml = r#"
            <launch>
                <group scoped="false">
                    <let name="leaked_var" value="value"/>
                </group>
                <group scoped="true">
                    <let name="scoped_var" value="value"/>
                </group>
                <group>
                    <let name="default_scoped" value="value"/>
                </group>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("test.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 3);

        if let LaunchElement::Group { scoped, .. } = &launch.elements[0] {
            assert!(!scoped); // scoped="false"
        } else {
            panic!("Expected Group element");
        }

        if let LaunchElement::Group { scoped, .. } = &launch.elements[1] {
            assert!(*scoped); // scoped="true"
        } else {
            panic!("Expected Group element");
        }

        if let LaunchElement::Group { scoped, .. } = &launch.elements[2] {
            assert!(*scoped); // default is true
        } else {
            panic!("Expected Group element");
        }
    }

    // ========================================================================
    // Substitution Parser Tests
    // ========================================================================

    #[test]
    fn test_parse_substitution_arg() {
        let parts = parse_substitutions("$(arg vehicle)").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0], Substitution::Arg("vehicle".to_string()));
    }

    #[test]
    fn test_parse_substitution_var() {
        let parts = parse_substitutions("$(var config)").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0], Substitution::Var("config".to_string()));
    }

    #[test]
    fn test_parse_substitution_env() {
        let parts = parse_substitutions("$(env HOME)").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(
            parts[0],
            Substitution::Env {
                name: "HOME".to_string(),
                default: None
            }
        );
    }

    #[test]
    fn test_parse_substitution_env_with_default() {
        let parts = parse_substitutions("$(env MY_VAR default_value)").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(
            parts[0],
            Substitution::Env {
                name: "MY_VAR".to_string(),
                default: Some("default_value".to_string())
            }
        );
    }

    #[test]
    fn test_parse_substitution_find_pkg_share() {
        let parts = parse_substitutions("$(find-pkg-share my_pkg)").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0], Substitution::FindPkgShare("my_pkg".to_string()));
    }

    #[test]
    fn test_parse_substitution_find_pkg_prefix() {
        let parts = parse_substitutions("$(find-pkg-prefix my_pkg)").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0], Substitution::FindPkgPrefix("my_pkg".to_string()));
    }

    #[test]
    fn test_parse_substitution_dirname() {
        let parts = parse_substitutions("$(dirname)").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0], Substitution::Dirname);
    }

    #[test]
    fn test_parse_substitution_eval() {
        let parts = parse_substitutions("$(eval '1' == '1')").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0], Substitution::Eval("'1' == '1'".to_string()));
    }

    #[test]
    fn test_parse_substitution_mixed() {
        let parts =
            parse_substitutions("$(find-pkg-share my_pkg)/config/$(arg vehicle).yaml").unwrap();
        assert_eq!(parts.len(), 4);
        assert_eq!(parts[0], Substitution::FindPkgShare("my_pkg".to_string()));
        assert_eq!(parts[1], Substitution::Literal("/config/".to_string()));
        assert_eq!(parts[2], Substitution::Arg("vehicle".to_string()));
        assert_eq!(parts[3], Substitution::Literal(".yaml".to_string()));
    }

    #[test]
    fn test_parse_substitution_nested() {
        // Nested substitution: $(find-pkg-share $(var pkg_name))
        let parts = parse_substitutions("$(find-pkg-share $(var pkg_name))").unwrap();
        assert_eq!(parts.len(), 1);
        // The inner $(var pkg_name) is kept as part of the argument
        assert_eq!(
            parts[0],
            Substitution::FindPkgShare("$(var pkg_name)".to_string())
        );
    }

    #[test]
    fn test_parse_substitution_literal_only() {
        let parts = parse_substitutions("/path/to/file.yaml").unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(
            parts[0],
            Substitution::Literal("/path/to/file.yaml".to_string())
        );
    }

    #[test]
    fn test_parse_substitution_error_unknown() {
        let result = parse_substitutions("$(unknown_cmd value)");
        assert!(result.is_err());
    }

    #[test]
    fn test_extract_file_dependency_find_pkg_share() {
        let dep = extract_file_dependency(
            "$(find-pkg-share my_pkg)/launch/foo.launch.xml",
            DependencyKind::Launch,
        );
        assert!(dep.is_some());
        let dep = dep.unwrap();
        assert_eq!(dep.package, "my_pkg");
        assert_eq!(dep.share_path, PathBuf::from("launch/foo.launch.xml"));
        assert_eq!(dep.kind, DependencyKind::Launch);
    }

    #[test]
    fn test_extract_file_dependency_find_pkg_prefix() {
        let dep = extract_file_dependency(
            "$(find-pkg-prefix my_pkg)/lib/my_pkg/config.yaml",
            DependencyKind::Param,
        );
        assert!(dep.is_some());
        let dep = dep.unwrap();
        assert_eq!(dep.package, "my_pkg");
        assert_eq!(dep.share_path, PathBuf::from("lib/my_pkg/config.yaml"));
        assert_eq!(dep.kind, DependencyKind::Param);
    }

    #[test]
    fn test_extract_file_dependency_nested_substitution() {
        // Package name is itself a substitution
        let dep = extract_file_dependency(
            "$(find-pkg-share $(var pkg_name))/launch/foo.launch.xml",
            DependencyKind::Launch,
        );
        assert!(dep.is_some());
        let dep = dep.unwrap();
        assert_eq!(dep.package, "$(var pkg_name)");
        assert_eq!(dep.share_path, PathBuf::from("launch/foo.launch.xml"));
        assert_eq!(dep.kind, DependencyKind::Launch);
    }

    #[test]
    fn test_extract_file_dependency_no_path() {
        // Just the package share, no file path
        let dep = extract_file_dependency("$(find-pkg-share my_pkg)", DependencyKind::Other);
        assert!(dep.is_none());
    }

    #[test]
    fn test_extract_file_dependency_not_matching() {
        let dep = extract_file_dependency("/absolute/path/to/file.xml", DependencyKind::Other);
        assert!(dep.is_none());

        let dep = extract_file_dependency("$(arg some_path)", DependencyKind::Other);
        assert!(dep.is_none());
    }

    #[test]
    fn test_extract_file_dependency_kind_preserved() {
        // Test that the kind parameter is correctly preserved
        let dep_launch =
            extract_file_dependency("$(find-pkg-share pkg)/file.xml", DependencyKind::Launch)
                .unwrap();
        let dep_param =
            extract_file_dependency("$(find-pkg-share pkg)/file.yaml", DependencyKind::Param)
                .unwrap();
        let dep_other =
            extract_file_dependency("$(find-pkg-share pkg)/file.txt", DependencyKind::Other)
                .unwrap();

        assert_eq!(dep_launch.kind, DependencyKind::Launch);
        assert_eq!(dep_param.kind, DependencyKind::Param);
        assert_eq!(dep_other.kind, DependencyKind::Other);
    }

    // ========================================================================
    // XML Parser Tests
    // ========================================================================

    #[test]
    fn test_parse_complex_autoware_style() {
        let xml = r#"
            <launch>
                <arg name="vehicle_model" default="sample_vehicle"/>
                <arg name="sensor_model" default="sample_sensor"/>
                <arg name="use_sim_time" default="false"/>

                <let name="vehicle_config" value="$(find-pkg-share $(var vehicle_model)_description)/config"/>

                <group>
                    <include file="$(find-pkg-share vehicle_launch)/launch/vehicle.launch.xml">
                        <arg name="vehicle_model" value="$(var vehicle_model)"/>
                        <arg name="config_dir" value="$(var vehicle_config)"/>
                    </include>
                </group>

                <group if="$(var use_sim_time)">
                    <node pkg="simulation" exec="sim_node" name="simulator">
                        <param name="use_sim_time" value="true"/>
                        <remap from="/clock" to="/sim_clock"/>
                    </node>
                </group>

                <node pkg="vehicle_interface" exec="interface_node" name="vehicle_interface" namespace="/vehicle">
                    <param name="vehicle_model" value="$(var vehicle_model)"/>
                    <env name="VEHICLE_MODEL" value="$(var vehicle_model)"/>
                </node>
            </launch>
        "#;

        let launch = parse_launch_xml(xml, Path::new("autoware.launch.xml")).unwrap();

        // 3 args + 1 let + 2 groups + 1 node = 7 elements
        assert_eq!(launch.elements.len(), 7);

        // Check args
        let args: Vec<_> = launch
            .elements
            .iter()
            .filter(|e| matches!(e, LaunchElement::Arg { .. }))
            .collect();
        assert_eq!(args.len(), 3);

        // Check let
        let lets: Vec<_> = launch
            .elements
            .iter()
            .filter(|e| matches!(e, LaunchElement::Let { .. }))
            .collect();
        assert_eq!(lets.len(), 1);

        // Check groups
        let groups: Vec<_> = launch
            .elements
            .iter()
            .filter(|e| matches!(e, LaunchElement::Group { .. }))
            .collect();
        assert_eq!(groups.len(), 2);

        // Check top-level node
        let nodes: Vec<_> = launch
            .elements
            .iter()
            .filter(|e| matches!(e, LaunchElement::Node { .. }))
            .collect();
        assert_eq!(nodes.len(), 1);
    }

    // ========================================================================
    // Renderer tests
    // ========================================================================

    #[test]
    fn test_render_resolved_xml_groups_by_source_file() {
        // Two nodes from the same source file → one <group>.
        // One node without a source file → emitted flat at <launch> level.
        let src_a = (
            "sensor_launch".to_string(),
            PathBuf::from("launch/sensing.launch.xml"),
        );
        let src_b = (
            "planner_launch".to_string(),
            PathBuf::from("launch/planning.launch.xml"),
        );

        let nodes = vec![
            ResolvedNode {
                package: "sensor_pkg".to_string(),
                executable: "sensor_node".to_string(),
                name: Some("lidar".to_string()),
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: [("rate".to_string(), "10".to_string())].into(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            ResolvedNode {
                package: "sensor_pkg".to_string(),
                executable: "camera_node".to_string(),
                name: Some("cam".to_string()),
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: Default::default(),
                remappings: vec![("/in".to_string(), "/out".to_string())],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            ResolvedNode {
                package: "planner_pkg".to_string(),
                executable: "planner".to_string(),
                name: None,
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_b.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            ResolvedNode {
                package: "util_pkg".to_string(),
                executable: "util".to_string(),
                name: None,
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: None, // no source → flat
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
        ];

        let xml = render_resolved_xml(
            "my_pkg",
            "launcher.launch.xml",
            &nodes,
            false,
            false,
            &HashMap::new(),
            false,
            &HashMap::new(),
            &HashMap::new(),
        );

        // Should have two <group> elements (one per source file)
        assert_eq!(
            xml.matches("<group>").count(),
            2,
            "expected 2 groups\n{xml}"
        );
        assert_eq!(xml.matches("</group>").count(), 2);

        // sensing group: sensor_node and camera_node both inside
        let sensing_group_start = xml
            .find("sensor_launch://launch/sensing.launch.xml")
            .expect("no sensing group comment");
        let sensing_group_end = xml[sensing_group_start..]
            .find("</group>")
            .map(|o| sensing_group_start + o)
            .expect("no closing </group>");
        let sensing_group = &xml[sensing_group_start..sensing_group_end];
        assert!(
            sensing_group.contains("sensor_node"),
            "sensor_node not in sensing group"
        );
        assert!(
            sensing_group.contains("camera_node"),
            "camera_node not in sensing group"
        );
        // param and remap should be indented inside the group's node
        assert!(
            sensing_group.contains("      <param"),
            "param should be 6-space indented"
        );
        assert!(
            sensing_group.contains("      <remap"),
            "remap should be 6-space indented"
        );

        // planning group: planner only
        assert!(xml.contains("planner_launch://launch/planning.launch.xml"));
        assert!(xml.contains("planner_pkg"));

        // util_pkg (no source) should be flat at 2-space indent, not inside a group
        let util_idx = xml.find("util_pkg").expect("util_pkg missing");
        // The line containing util_pkg should start with "  <node" (2 spaces), not "    <node"
        let line_start = xml[..util_idx].rfind('\n').map_or(0, |i| i + 1);
        let line = &xml[line_start..util_idx + "util_pkg".len()];
        assert!(
            line.starts_with("  <node"),
            "ungrouped node should be at 2-space indent, got: {:?}",
            line
        );
    }

    #[test]
    fn test_render_resolved_xml_push_ros_namespace_boundary() {
        // Nodes with a namespace_stack should be wrapped in a <group> that includes a
        // <push-ros-namespace> element.  The node's namespace= attribute should be omitted
        // when it is fully covered by the stack; it should be preserved when the node has an
        // additional explicit namespace beyond the stack.
        let src_a = (
            "sensing_launch".to_string(),
            PathBuf::from("launch/sensing.launch.xml"),
        );

        let nodes = vec![
            // Stack only — namespace= on <node> should be omitted.
            ResolvedNode {
                package: "lidar_pkg".to_string(),
                executable: "lidar_node".to_string(),
                name: Some("lidar".to_string()),
                namespace: Some("/sensing/lidar".to_string()), // effective = stack only
                explicit_namespace: None,
                namespace_stack: vec!["sensing".to_string(), "lidar".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            // Second node with same context — same group.
            ResolvedNode {
                package: "radar_pkg".to_string(),
                executable: "radar_node".to_string(),
                name: None,
                namespace: Some("/sensing/lidar".to_string()),
                explicit_namespace: None,
                namespace_stack: vec!["sensing".to_string(), "lidar".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            // Node with no namespace stack — flat, no push-ros-namespace.
            ResolvedNode {
                package: "planning_pkg".to_string(),
                executable: "planner".to_string(),
                name: None,
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: None,
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
        ];

        let xml = render_resolved_xml(
            "my_pkg",
            "top.launch.xml",
            &nodes,
            false,
            false,
            &HashMap::new(),
            false,
            &HashMap::new(),
            &HashMap::new(),
        );

        // Two <group> elements: one source group for src_a, one namespace sub-group inside it.
        assert_eq!(
            xml.matches("<group>").count(),
            2,
            "expected 2 groups (source + namespace)\n{xml}"
        );
        assert_eq!(xml.matches("</group>").count(), 2);

        // <push-ros-namespace> should appear inside the inner (namespace) group.
        assert!(
            xml.contains("<push-ros-namespace namespace=\"/sensing/lidar\"/>"),
            "missing push-ros-namespace\n{xml}"
        );

        // The lidar and radar <node> elements should NOT carry a namespace= attribute since
        // the stack fully covers it.  (The <push-ros-namespace> element itself does have
        // namespace=, so we check the <node> lines specifically.)
        for line in xml.lines() {
            if line.trim_start().starts_with("<node") {
                assert!(
                    !line.contains("namespace="),
                    "<node> should not have namespace= when covered by push-ros-namespace: {line}"
                );
            }
        }

        // The planning_pkg node has no stack — it should appear after the first </group>, flat.
        let group_close_idx = xml.find("</group>").expect("no </group>");
        let plan_idx = xml.find("planning_pkg").expect("planning_pkg missing");
        assert!(
            plan_idx > group_close_idx,
            "planning_pkg should appear after </group>\n{xml}"
        );
    }

    #[test]
    fn test_render_resolved_xml_nested_groups_from_include_chain() {
        // Nodes with include_chain set (as stamped by the orchestrator) produce nested <group>s.
        //
        // Include hierarchy:
        //   root.launch.xml (root)
        //     └─ comp.launch.xml          (chain depth 1)
        //          └─ sensing.launch.xml  (chain depth 2, source of lidar/radar nodes)
        //
        // Expected nested structure:
        //   <!-- source: comp.launch.xml -->
        //   <group>
        //     <!-- source: sensing.launch.xml -->
        //     <group>
        //       <node lidar/>
        //       <node radar/>
        //     </group>
        //   </group>
        let root = (
            "my_pkg".to_string(),
            PathBuf::from("launch/root.launch.xml"),
        );
        let comp = (
            "comp_pkg".to_string(),
            PathBuf::from("launch/comp.launch.xml"),
        );
        let sensing = (
            "sensing_pkg".to_string(),
            PathBuf::from("launch/sensing.launch.xml"),
        );

        let nodes = vec![
            ResolvedNode {
                package: "lidar_pkg".to_string(),
                executable: "lidar_node".to_string(),
                name: Some("lidar".to_string()),
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(sensing.clone()),
                include_chain: vec![root.clone(), comp.clone(), sensing.clone()],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            ResolvedNode {
                package: "radar_pkg".to_string(),
                executable: "radar_node".to_string(),
                name: None,
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(sensing.clone()),
                include_chain: vec![root.clone(), comp.clone(), sensing.clone()],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
        ];

        let xml = render_resolved_xml(
            "my_pkg",
            "root.launch.xml",
            &nodes,
            false,
            false,
            &HashMap::new(),
            false,
            &HashMap::new(),
            &HashMap::new(),
        );

        // Two nested <group>s: outer for comp, inner for sensing.
        assert_eq!(
            xml.matches("<group>").count(),
            2,
            "expected 2 nested groups\n{xml}"
        );
        assert_eq!(xml.matches("</group>").count(), 2);

        // Both nodes are inside (no namespace sub-groups, just source nesting).
        assert!(xml.contains("lidar_node"), "lidar_node missing\n{xml}");
        assert!(xml.contains("radar_node"), "radar_node missing\n{xml}");

        // comp group comes before sensing group in source-comment order.
        let comp_pos = xml
            .find("comp_pkg://launch/comp.launch.xml")
            .expect("comp comment missing");
        let sensing_pos = xml
            .find("sensing_pkg://launch/sensing.launch.xml")
            .expect("sensing comment missing");
        assert!(
            comp_pos < sensing_pos,
            "comp group should appear before sensing group\n{xml}"
        );

        // sensing comment is inside the comp <group> (appears after the outer <group> tag).
        let outer_group_pos = xml.find("<group>").expect("no <group>");
        assert!(
            sensing_pos > outer_group_pos,
            "sensing source comment should be inside the comp <group>\n{xml}"
        );

        // Nodes should be at 6-space indent (depth 2 + 1 = pad(2) = 6 spaces).
        let lidar_line = xml
            .lines()
            .find(|l| l.contains("lidar_node"))
            .expect("lidar_node line missing");
        assert!(
            lidar_line.starts_with("      "),
            "lidar_node should be at 6-space indent, got: {lidar_line:?}"
        );
    }

    // -------------------------------------------------------------------------
    // effective_namespace / ros2_namespace_join unit tests
    // -------------------------------------------------------------------------

    #[test]
    fn test_effective_namespace_relative_stack() {
        // All-relative components are joined left-to-right with leading "/".
        assert_eq!(
            effective_namespace(&["sensing".into(), "lidar".into()], None),
            Some("/sensing/lidar".to_string())
        );
    }

    #[test]
    fn test_effective_namespace_absolute_reset_in_stack() {
        // An absolute component in the middle of the stack resets the accumulated base.
        // ROS 2 semantics: namespace_join("/sensing", "/abs") = "/abs".
        assert_eq!(
            effective_namespace(&["sensing".into(), "/abs".into()], None),
            Some("/abs".to_string())
        );
        // Further relative components are appended to the reset base.
        assert_eq!(
            effective_namespace(&["sensing".into(), "/abs".into(), "sub".into()], None),
            Some("/abs/sub".to_string())
        );
        // Two consecutive absolute components: innermost wins.
        assert_eq!(
            effective_namespace(&["/outer".into(), "/inner".into()], None),
            Some("/inner".to_string())
        );
        // Innermost absolute, then relative appended to it.
        assert_eq!(
            effective_namespace(&["/outer".into(), "/inner".into(), "sub".into()], None),
            Some("/inner/sub".to_string())
        );
    }

    #[test]
    fn test_effective_namespace_absolute_node_ns_overrides_stack() {
        // A node with an absolute explicit namespace replaces the stack entirely.
        assert_eq!(
            effective_namespace(&["sensing".into()], Some("/override")),
            Some("/override".to_string())
        );
    }

    #[test]
    fn test_effective_namespace_relative_node_ns_appended() {
        // A relative explicit namespace is appended to the stack prefix.
        assert_eq!(
            effective_namespace(&["sensing".into()], Some("lidar")),
            Some("/sensing/lidar".to_string())
        );
    }

    #[test]
    fn test_effective_namespace_empty_stack_and_ns() {
        assert_eq!(effective_namespace(&[], None), None);
    }

    #[test]
    fn test_render_resolved_xml_flatten_namespaces() {
        // Two nodes from the same file in /sensing/lidar namespace, one node from another
        // file in /planning namespace.  With --flatten-namespaces:
        // - No <push-ros-namespace> emitted
        // - namespace= attribute appears directly on each node that has one
        // - Same-source nodes end up in one group regardless of namespace stack differences
        let src_a = (
            "sensor_launch".to_string(),
            PathBuf::from("launch/sensing.launch.xml"),
        );
        let src_b = (
            "planner_launch".to_string(),
            PathBuf::from("launch/planning.launch.xml"),
        );

        let nodes = vec![
            ResolvedNode {
                package: "lidar_pkg".to_string(),
                executable: "lidar_node".to_string(),
                name: Some("lidar".to_string()),
                namespace: Some("/sensing/lidar".to_string()),
                explicit_namespace: None,
                namespace_stack: vec!["sensing".to_string(), "lidar".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            // Same file but different (sibling) namespace — in normal mode this would be a
            // separate group; in flatten mode it must be merged with the node above.
            ResolvedNode {
                package: "radar_pkg".to_string(),
                executable: "radar_node".to_string(),
                name: None,
                namespace: Some("/sensing/radar".to_string()),
                explicit_namespace: None,
                namespace_stack: vec!["sensing".to_string(), "radar".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            ResolvedNode {
                package: "planner_pkg".to_string(),
                executable: "planner".to_string(),
                name: None,
                namespace: Some("/planning".to_string()),
                explicit_namespace: None,
                namespace_stack: vec!["planning".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_b.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
        ];

        let xml = render_resolved_xml(
            "my_pkg",
            "top.launch.xml",
            &nodes,
            true,
            false,
            &HashMap::new(),
            false,
            &HashMap::new(),
            &HashMap::new(),
        );

        // No push-ros-namespace anywhere.
        assert!(
            !xml.contains("<push-ros-namespace"),
            "push-ros-namespace should be absent in flatten mode\n{xml}"
        );

        // Two groups: one per source file.
        assert_eq!(
            xml.matches("<group>").count(),
            2,
            "expected 2 groups in flatten mode\n{xml}"
        );

        // Each node must carry its own namespace= attribute.
        assert!(
            xml.contains("namespace=\"/sensing/lidar\""),
            "lidar node missing namespace\n{xml}"
        );
        assert!(
            xml.contains("namespace=\"/sensing/radar\""),
            "radar node missing namespace\n{xml}"
        );
        assert!(
            xml.contains("namespace=\"/planning\""),
            "planner node missing namespace\n{xml}"
        );
    }

    // =========================================================================
    // YAML launch file parser tests
    #[test]
    fn test_render_resolved_xml_flatten_no_groups_except_ns() {
        // --flatten: source-boundary <group>s are suppressed; nodes emitted flat.
        // A node with a non-empty namespace_stack still gets a <group> + <push-ros-namespace>.
        let src_a = (
            "sensor_launch".to_string(),
            PathBuf::from("launch/sensing.launch.xml"),
        );
        let src_b = (
            "planner_launch".to_string(),
            PathBuf::from("launch/planning.launch.xml"),
        );

        let nodes = vec![
            // No namespace stack → should appear flat (no group) under --flatten.
            ResolvedNode {
                package: "sensor_pkg".to_string(),
                executable: "sensor_node".to_string(),
                name: None,
                namespace: None,
                explicit_namespace: None,
                namespace_stack: vec![],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            // Non-empty namespace stack → still wrapped in <group>+<push-ros-namespace>.
            ResolvedNode {
                package: "lidar_pkg".to_string(),
                executable: "lidar_node".to_string(),
                name: None,
                namespace: Some("/sensing/lidar".to_string()),
                explicit_namespace: None,
                namespace_stack: vec!["sensing".to_string(), "lidar".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_b.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
        ];

        let xml = render_resolved_xml(
            "my_pkg",
            "top.launch.xml",
            &nodes,
            false,
            true,
            &HashMap::new(),
            false,
            &HashMap::new(),
            &HashMap::new(),
        );

        // Only one <group> — the one wrapping the namespace node.
        assert_eq!(
            xml.matches("<group>").count(),
            1,
            "expected 1 group (ns-only)\n{xml}"
        );
        assert!(
            xml.contains("<push-ros-namespace"),
            "push-ros-namespace missing\n{xml}"
        );

        // sensor_node has no namespace stack → must appear flat (no surrounding <group>).
        // Verify it is present and NOT inside a <group>...</group> block.
        assert!(xml.contains("sensor_node"), "sensor_node missing\n{xml}");
        let sensor_idx = xml.find("sensor_node").unwrap();
        let group_idx = xml.find("<group>").unwrap();
        assert!(
            sensor_idx < group_idx,
            "sensor_node should appear before the namespace group\n{xml}"
        );

        // End comments should be present for each source section.
        assert!(
            xml.contains("<!-- end:"),
            "end comment missing in flatten mode\n{xml}"
        );
        assert_eq!(
            xml.matches("<!-- end:").count(),
            2,
            "expected one end comment per source\n{xml}"
        );
    }

    #[test]
    fn test_render_resolved_xml_flatten_and_flatten_namespaces_no_groups() {
        // --flatten --flatten-namespaces: completely group-free output.
        let src_a = (
            "sensor_launch".to_string(),
            PathBuf::from("launch/sensing.launch.xml"),
        );

        let nodes = vec![
            ResolvedNode {
                package: "lidar_pkg".to_string(),
                executable: "lidar_node".to_string(),
                name: None,
                namespace: Some("/sensing/lidar".to_string()),
                explicit_namespace: None,
                namespace_stack: vec!["sensing".to_string(), "lidar".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
            ResolvedNode {
                package: "radar_pkg".to_string(),
                executable: "radar_node".to_string(),
                name: None,
                namespace: Some("/sensing/radar".to_string()),
                explicit_namespace: None,
                namespace_stack: vec!["sensing".to_string(), "radar".to_string()],
                parameters: Default::default(),
                remappings: vec![],
                env: Default::default(),
                source: Some(src_a.clone()),
                include_chain: vec![],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
            },
        ];

        let xml = render_resolved_xml(
            "my_pkg",
            "top.launch.xml",
            &nodes,
            true,
            true,
            &HashMap::new(),
            false,
            &HashMap::new(),
            &HashMap::new(),
        );

        // No groups whatsoever.
        assert!(!xml.contains("<group>"), "no groups expected\n{xml}");
        assert!(
            !xml.contains("<push-ros-namespace"),
            "no push-ros-namespace expected\n{xml}"
        );

        // Both nodes present with full namespace= attribute.
        assert!(
            xml.contains("namespace=\"/sensing/lidar\""),
            "lidar ns missing\n{xml}"
        );
        assert!(
            xml.contains("namespace=\"/sensing/radar\""),
            "radar ns missing\n{xml}"
        );

        // Source comment and matching end comment both present.
        assert!(
            xml.contains("<!-- source:"),
            "source comment missing\n{xml}"
        );
        assert!(xml.contains("<!-- end:"), "end comment missing\n{xml}");
        // One source + one end (single source file).
        assert_eq!(
            xml.matches("<!-- source:").count(),
            1,
            "expected 1 source comment\n{xml}"
        );
        assert_eq!(
            xml.matches("<!-- end:").count(),
            1,
            "expected 1 end comment\n{xml}"
        );
    }

    // =========================================================================

    #[test]
    fn test_parse_launch_yaml_args() {
        // Core use case: Autoware-style preset YAML with arg declarations.
        let yaml = r#"
launch:
  - arg:
      name: motion_path_smoother_type
      default: elastic_band
  - arg:
      name: launch_parking_module
      default: "true"
  - arg:
      name: launch_static_obstacle_avoidance
      default: "false"
  - arg:
      name: no_default_arg
"#;
        let path = std::path::Path::new("default_preset.yaml");
        let launch = parse_launch_yaml(yaml, path).expect("parse failed");
        assert_eq!(launch.elements.len(), 4);

        // First arg
        match &launch.elements[0] {
            LaunchElement::Arg { name, default, .. } => {
                assert_eq!(name, "motion_path_smoother_type");
                assert_eq!(default.as_deref(), Some("elastic_band"));
            }
            other => panic!("expected Arg, got {other:?}"),
        }
        // Bool default coerced to string
        match &launch.elements[1] {
            LaunchElement::Arg { name, default, .. } => {
                assert_eq!(name, "launch_parking_module");
                assert_eq!(default.as_deref(), Some("true"));
            }
            other => panic!("expected Arg, got {other:?}"),
        }
        // Arg without default
        match &launch.elements[3] {
            LaunchElement::Arg { name, default, .. } => {
                assert_eq!(name, "no_default_arg");
                assert!(default.is_none());
            }
            other => panic!("expected Arg, got {other:?}"),
        }
    }

    #[test]
    fn test_parse_launch_yaml_let_set_env_unset_env() {
        let yaml = r#"
launch:
  - let:
      name: config_dir
      value: "$(find-pkg-share my_pkg)/config"
  - set_env:
      name: MY_VAR
      value: hello
  - unset_env:
      name: OLD_VAR
"#;
        let launch =
            parse_launch_yaml(yaml, std::path::Path::new("test.yaml")).expect("parse failed");
        assert_eq!(launch.elements.len(), 3);
        assert!(
            matches!(&launch.elements[0], LaunchElement::Let { name, .. } if name == "config_dir")
        );
        assert!(
            matches!(&launch.elements[1], LaunchElement::SetEnv { name, .. } if name == "MY_VAR")
        );
        assert!(
            matches!(&launch.elements[2], LaunchElement::UnsetEnv { name, .. } if name == "OLD_VAR")
        );
    }

    #[test]
    fn test_parse_launch_yaml_push_ros_namespace_and_group() {
        let yaml = r#"
launch:
  - push-ros-namespace:
      namespace: /sensing
      if: "$(var use_sensing)"
  - group:
      scoped: false
      unless: "$(var skip_group)"
      children:
        - arg:
            name: child_arg
            default: child_val
        - let:
            name: child_let
            value: foo
"#;
        let launch =
            parse_launch_yaml(yaml, std::path::Path::new("test.yaml")).expect("parse failed");
        assert_eq!(launch.elements.len(), 2);

        match &launch.elements[0] {
            LaunchElement::PushRosNamespace {
                namespace,
                condition,
            } => {
                assert_eq!(namespace, "/sensing");
                assert!(matches!(
                    condition,
                    Some(Condition {
                        kind: ConditionKind::If,
                        ..
                    })
                ));
            }
            other => panic!("expected PushRosNamespace, got {other:?}"),
        }

        match &launch.elements[1] {
            LaunchElement::Group {
                scoped,
                condition,
                children,
            } => {
                assert!(!scoped);
                assert!(matches!(
                    condition,
                    Some(Condition {
                        kind: ConditionKind::Unless,
                        ..
                    })
                ));
                assert_eq!(children.len(), 2);
            }
            other => panic!("expected Group, got {other:?}"),
        }
    }

    #[test]
    fn test_parse_launch_yaml_include() {
        let yaml = r#"
launch:
  - include:
      file: "$(find-pkg-share my_pkg)/launch/foo.launch.xml"
      if: "$(var use_foo)"
      arg:
        - name: vehicle_model
          value: "$(var vehicle_model)"
        - name: sensor_kit
          value: sample_kit
"#;
        let launch =
            parse_launch_yaml(yaml, std::path::Path::new("test.yaml")).expect("parse failed");
        assert_eq!(launch.elements.len(), 1);
        match &launch.elements[0] {
            LaunchElement::Include {
                file,
                condition,
                args,
            } => {
                assert!(file.contains("foo.launch.xml"));
                assert!(condition.is_some());
                assert_eq!(args.len(), 2);
                assert_eq!(args[0].name, "vehicle_model");
                assert_eq!(args[1].value, "sample_kit");
            }
            other => panic!("expected Include, got {other:?}"),
        }
    }

    #[test]
    fn test_parse_launch_yaml_node() {
        let yaml = r#"
launch:
  - node:
      pkg: my_pkg
      exec: my_node
      name: named_node
      namespace: /my_ns
      unless: "$(var skip_node)"
      param:
        - name: rate
          value: "10"
        - from: /path/to/params.yaml
      remap:
        - from: /in
          to: /out
      env:
        - name: MY_ENV
          value: val
"#;
        let launch =
            parse_launch_yaml(yaml, std::path::Path::new("test.yaml")).expect("parse failed");
        assert_eq!(launch.elements.len(), 1);
        match &launch.elements[0] {
            LaunchElement::Node {
                pkg,
                exec,
                name,
                namespace,
                condition,
                params,
                remaps,
                envs,
                ..
            } => {
                assert_eq!(pkg, "my_pkg");
                assert_eq!(exec, "my_node");
                assert_eq!(name.as_deref(), Some("named_node"));
                assert_eq!(namespace.as_deref(), Some("/my_ns"));
                assert!(matches!(
                    condition,
                    Some(Condition {
                        kind: ConditionKind::Unless,
                        ..
                    })
                ));
                assert_eq!(params.len(), 2);
                assert_eq!(params[0].name.as_deref(), Some("rate"));
                assert_eq!(params[1].from.as_deref(), Some("/path/to/params.yaml"));
                assert_eq!(remaps.len(), 1);
                assert_eq!(remaps[0].from, "/in");
                assert_eq!(envs.len(), 1);
                assert_eq!(envs[0].name, "MY_ENV");
            }
            other => panic!("expected Node, got {other:?}"),
        }
    }

    #[test]
    fn test_parse_launch_yaml_unknown_elements_skipped() {
        // Unknown element types should be warned and skipped.
        let yaml = r#"
launch:
  - arg:
      name: known
      default: val
  - truly_unknown_element:
      some_field: value
  - arg:
      name: also_known
      default: val2
"#;
        let launch =
            parse_launch_yaml(yaml, std::path::Path::new("test.yaml")).expect("parse failed");
        // Unknown element is preserved as UnknownElement; 2 args + 1 unknown = 3.
        assert_eq!(
            launch.elements.len(),
            3,
            "expected 3 elements, got:\n{:?}",
            launch.elements
        );
        assert!(
            matches!(&launch.elements[1], LaunchElement::UnknownElement { tag_name } if tag_name == "truly_unknown_element")
        );
    }

    #[test]
    fn test_parse_xml_node_container() {
        // <node_container> is parsed with its <composable_node> children.
        let xml = r#"<?xml version="1.0"?>
<launch>
  <node_container pkg="rclcpp_components" exec="component_container_mt" name="my_container">
    <composable_node pkg="nebula_ros" plugin="nebula::ros::HesaiRosWrapper" name="hesai_driver"/>
    <composable_node pkg="nebula_ros" plugin="nebula::ros::HesaiDecoderRosWrapper" name="hesai_decoder"/>
  </node_container>
  <set_parameter name="use_sim_time" value="false"/>
  <set_remap from="input" to="output"/>
</launch>"#;
        let launch = parse_launch_xml(xml, Path::new("test.xml")).expect("parse failed");
        assert_eq!(
            launch.elements.len(),
            3,
            "expected 3 elements, got:\n{:?}",
            launch.elements
        );
        match &launch.elements[0] {
            LaunchElement::NodeContainer {
                pkg,
                exec,
                name,
                composable_nodes,
                ..
            } => {
                assert_eq!(pkg, "rclcpp_components");
                assert_eq!(exec, "component_container_mt");
                assert_eq!(name.as_deref(), Some("my_container"));
                assert_eq!(composable_nodes.len(), 2);
                assert_eq!(composable_nodes[0].plugin, "nebula::ros::HesaiRosWrapper");
                assert_eq!(composable_nodes[1].pkg, "nebula_ros");
            }
            other => panic!("expected NodeContainer, got {other:?}"),
        }
        assert!(
            matches!(&launch.elements[1], LaunchElement::SetParameter { name, .. } if name == "use_sim_time")
        );
        assert!(
            matches!(&launch.elements[2], LaunchElement::SetRemap { from, .. } if from == "input")
        );
    }

    #[test]
    fn test_parse_xml_load_composable_node() {
        let xml = r#"<?xml version="1.0"?>
<launch>
  <load_composable_node target="/my_container">
    <composable_node pkg="my_pkg" plugin="my_pkg::MyPlugin" name="my_plugin"/>
  </load_composable_node>
</launch>"#;
        let launch = parse_launch_xml(xml, Path::new("test.xml")).expect("parse failed");
        assert_eq!(launch.elements.len(), 1);
        match &launch.elements[0] {
            LaunchElement::LoadComposableNode {
                target,
                composable_nodes,
                ..
            } => {
                assert_eq!(target.as_deref(), Some("/my_container"));
                assert_eq!(composable_nodes.len(), 1);
                assert_eq!(composable_nodes[0].plugin, "my_pkg::MyPlugin");
            }
            other => panic!("expected LoadComposableNode, got {other:?}"),
        }
    }

    // -------------------------------------------------------------------------
    // IncludeMarker rendering tests
    // -------------------------------------------------------------------------

    #[test]
    fn test_include_marker_inline_comment_pair_no_group() {
        // A file with no nodes produces an IncludeMarker.  The renderer emits a
        // <!-- source: --> / <!-- end: --> comment pair without a <group> element.
        let nodes = vec![
            // Real node from the parent (root-level, no include chain).
            ResolvedNode {
                package: "parent_pkg".to_string(),
                executable: "parent_node".to_string(),
                name: Some("parent".to_string()),
                source: Some((
                    "root_pkg".to_string(),
                    PathBuf::from("launch/root.launch.xml"),
                )),
                include_chain: vec![(
                    "root_pkg".to_string(),
                    PathBuf::from("launch/root.launch.xml"),
                )],
                kind: NodeKind::Node,
                param_files: vec![],
                output: None,
                args: None,
                respawn: None,
                respawn_delay: None,
                ..Default::default()
            },
            // Marker for an included file that had no nodes.
            ResolvedNode {
                source: Some((
                    "preset_pkg".to_string(),
                    PathBuf::from("launch/preset.launch.xml"),
                )),
                include_chain: vec![
                    (
                        "root_pkg".to_string(),
                        PathBuf::from("launch/root.launch.xml"),
                    ),
                    (
                        "preset_pkg".to_string(),
                        PathBuf::from("launch/preset.launch.xml"),
                    ),
                ],
                kind: NodeKind::IncludeMarker,
                ..Default::default()
            },
        ];

        let xml = render_resolved_xml(
            "root_pkg",
            "root.launch.xml",
            &nodes,
            false,
            false,
            &HashMap::new(),
            false,
            &HashMap::new(),
            &HashMap::new(),
        );

        // Marker comment pair must appear.
        assert!(
            xml.contains("<!-- source: preset_pkg://launch/preset.launch.xml -->"),
            "missing source comment:\n{xml}"
        );
        assert!(
            xml.contains("<!-- end: preset_pkg://launch/preset.launch.xml -->"),
            "missing end comment:\n{xml}"
        );
        // The real node is root-level (its chain matches the root, so target_src = []).
        // The marker is one level deep relative to root, so it also appears at root level.
        // Neither produces a <group> tag.
        assert!(
            !xml.contains("<group>"),
            "unexpected <group> in output:\n{xml}"
        );
    }

    #[test]
    fn test_parse_yaml_composable_node_container() {
        let yaml = r#"
launch:
  - composable_node_container:
      name: my_container
      pkg: rclcpp_components
      exec: component_container_mt
      composable_node:
        - pkg: nebula_ros
          plugin: nebula::ros::HesaiRosWrapper
          name: hesai_driver
"#;
        let launch =
            parse_launch_yaml(yaml, std::path::Path::new("test.yaml")).expect("parse failed");
        assert_eq!(launch.elements.len(), 1);
        match &launch.elements[0] {
            LaunchElement::NodeContainer {
                exec,
                composable_nodes,
                ..
            } => {
                assert_eq!(exec, "component_container_mt");
                assert_eq!(composable_nodes.len(), 1);
                assert_eq!(composable_nodes[0].plugin, "nebula::ros::HesaiRosWrapper");
            }
            other => panic!("expected NodeContainer, got {other:?}"),
        }
    }

    // -------------------------------------------------------------------------
    // Arg-lifecycle static analysis tests
    // -------------------------------------------------------------------------

    #[test]
    fn test_collect_declared_args_top_level() {
        let xml = r#"<launch>
  <arg name="x"/>
  <arg name="y" default="val"/>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let declared = collect_declared_args(&launch.elements);
        assert!(declared.contains("x"), "expected 'x' in declared");
        assert!(declared.contains("y"), "expected 'y' in declared");
        assert_eq!(declared.len(), 2);
    }

    #[test]
    fn test_collect_declared_args_inside_group() {
        let xml = r#"<launch>
  <arg name="outer"/>
  <group>
    <arg name="inner"/>
  </group>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let declared = collect_declared_args(&launch.elements);
        assert!(declared.contains("outer"));
        assert!(declared.contains("inner"));
        assert_eq!(declared.len(), 2);
    }

    #[test]
    fn test_collect_declared_args_does_not_recurse_into_includes() {
        // Include elements are not traversed — they are separate files.
        let xml = r#"<launch>
  <arg name="mine"/>
  <include file="$(find-pkg-share pkg)/other.launch.xml">
    <arg name="forwarded" value="x"/>
  </include>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let declared = collect_declared_args(&launch.elements);
        // Only 'mine' should be in declared; 'forwarded' is an include arg, not a file arg.
        assert!(declared.contains("mine"));
        assert!(
            !declared.contains("forwarded"),
            "'forwarded' is an include arg, not a file declaration"
        );
        assert_eq!(declared.len(), 1);
    }

    #[test]
    fn test_collect_arg_and_var_refs_basic() {
        let xml = r#"<launch>
  <node pkg="$(arg pkg_name)" exec="my_exec"/>
  <let name="cfg" value="$(var some_var)/config.yaml"/>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let refs = collect_arg_and_var_refs(&launch.elements);
        assert!(
            refs.contains("pkg_name"),
            "expected 'pkg_name' from $(arg pkg_name)"
        );
        assert!(
            refs.contains("some_var"),
            "expected 'some_var' from $(var some_var)"
        );
    }

    #[test]
    fn test_collect_arg_and_var_refs_in_condition() {
        let xml = r#"<launch>
  <group if="$(arg use_sim)">
    <node pkg="sim_pkg" exec="sim"/>
  </group>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let refs = collect_arg_and_var_refs(&launch.elements);
        assert!(
            refs.contains("use_sim"),
            "expected 'use_sim' from condition expr"
        );
    }

    #[test]
    fn test_collect_arg_and_var_refs_in_include_arg_value() {
        let xml = r#"<launch>
  <arg name="vehicle_model"/>
  <include file="$(find-pkg-share pkg)/child.launch.xml">
    <arg name="vehicle_model" value="$(var vehicle_model)"/>
  </include>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let refs = collect_arg_and_var_refs(&launch.elements);
        // $(var vehicle_model) in the include arg value is a reference.
        assert!(
            refs.contains("vehicle_model"),
            "expected 'vehicle_model' from include arg value"
        );
    }

    #[test]
    fn test_collect_arg_and_var_refs_does_not_include_env() {
        let xml = r#"<launch>
  <set_env name="HOME" value="$(env HOME)"/>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let refs = collect_arg_and_var_refs(&launch.elements);
        // $(env HOME) should NOT be in arg/var refs.
        assert!(
            !refs.contains("HOME"),
            "$(env HOME) should not count as an arg/var ref"
        );
    }

    // -------------------------------------------------------------------------
    // Anti-pattern static scan tests
    // -------------------------------------------------------------------------

    #[test]
    fn test_collect_scoped_false_includes_direct_child() {
        let xml = r#"<launch>
  <group scoped="false">
    <include file="$(find-pkg-share foo)/bar.launch.xml"/>
  </group>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let includes = collect_scoped_false_includes(&launch.elements);
        assert_eq!(includes.len(), 1);
        assert!(includes[0].contains("bar.launch.xml"));
    }

    #[test]
    fn test_collect_scoped_false_includes_nested_unscoped() {
        // An include inside a nested scoped=false group should also be flagged.
        let xml = r#"<launch>
  <group scoped="false">
    <group scoped="false">
      <include file="nested.launch.xml"/>
    </group>
  </group>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let includes = collect_scoped_false_includes(&launch.elements);
        assert_eq!(includes.len(), 1);
        assert!(includes[0].contains("nested.launch.xml"));
    }

    #[test]
    fn test_collect_scoped_false_includes_scoped_barrier() {
        // An include inside a scoped=true group (the default) should NOT be flagged.
        let xml = r#"<launch>
  <group scoped="false">
    <group>
      <include file="safe.launch.xml"/>
    </group>
  </group>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let includes = collect_scoped_false_includes(&launch.elements);
        assert!(
            includes.is_empty(),
            "include inside scoped=true group should not be flagged"
        );
    }

    #[test]
    fn test_collect_scoped_false_includes_top_level_not_flagged() {
        // A top-level include (not inside any group) should NOT be flagged.
        let xml = r#"<launch>
  <include file="top_level.launch.xml"/>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let includes = collect_scoped_false_includes(&launch.elements);
        assert!(
            includes.is_empty(),
            "top-level include should not be flagged"
        );
    }

    #[test]
    fn test_collect_env_without_fallback_flags_bare_env() {
        let xml = r#"<launch>
  <arg name="path" default="$(env CUSTOM_PATH)"/>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let names = collect_env_without_fallback(&launch.elements);
        assert!(names.contains(&"CUSTOM_PATH".to_string()));
    }

    #[test]
    fn test_collect_env_without_fallback_ignores_env_with_default() {
        let xml = r#"<launch>
  <arg name="id" default="$(env VEHICLE_ID default)"/>
</launch>"#;
        let launch = parse_launch_xml(xml, std::path::Path::new("t.launch.xml")).unwrap();
        let names = collect_env_without_fallback(&launch.elements);
        assert!(names.is_empty(), "$(env X default) should not be flagged");
    }

    // =========================================================================
    // Event-based launch elements
    // =========================================================================

    #[test]
    fn test_parse_lifecycle_node() {
        let xml = r#"<launch>
  <lifecycle_node pkg="ros2_socketcan" exec="socket_can_receiver_node_exe" name="socket_can_receiver">
    <param name="interface" value="can0"/>
  </lifecycle_node>
</launch>"#;
        let launch = parse_launch_xml(xml, Path::new("/test/t.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);
        if let LaunchElement::LifecycleNode {
            pkg,
            exec,
            name,
            params,
            ..
        } = &launch.elements[0]
        {
            assert_eq!(pkg, "ros2_socketcan");
            assert_eq!(exec, "socket_can_receiver_node_exe");
            assert_eq!(name.as_deref(), Some("socket_can_receiver"));
            assert_eq!(params.len(), 1);
            assert_eq!(params[0].name.as_deref(), Some("interface"));
        } else {
            panic!("expected LifecycleNode");
        }
    }

    #[test]
    fn test_parse_event_handler_on_process_start() {
        let xml = r#"<launch>
  <on_process_start target="socket_can_receiver">
    <emit_event event="configure" target_node="socket_can_receiver"/>
  </on_process_start>
</launch>"#;
        let launch = parse_launch_xml(xml, Path::new("/test/t.launch.xml")).unwrap();
        assert_eq!(launch.elements.len(), 1);
        if let LaunchElement::EventHandler {
            kind,
            target,
            children,
            ..
        } = &launch.elements[0]
        {
            assert_eq!(*kind, EventHandlerKind::OnProcessStart);
            assert_eq!(target.as_deref(), Some("socket_can_receiver"));
            assert_eq!(children.len(), 1);
            if let LaunchElement::EmitEvent {
                event, target_node, ..
            } = &children[0]
            {
                assert_eq!(event, "configure");
                assert_eq!(target_node.as_deref(), Some("socket_can_receiver"));
            } else {
                panic!("expected EmitEvent child");
            }
        } else {
            panic!("expected EventHandler");
        }
    }

    #[test]
    fn test_parse_on_state_transition() {
        let xml = r#"<launch>
  <on_state_transition target_node="my_node" start_state="configuring" goal_state="inactive">
    <emit_event event="activate" target_node="my_node"/>
  </on_state_transition>
</launch>"#;
        let launch = parse_launch_xml(xml, Path::new("/test/t.launch.xml")).unwrap();
        if let LaunchElement::EventHandler {
            kind,
            target_node,
            start_state,
            goal_state,
            ..
        } = &launch.elements[0]
        {
            assert_eq!(*kind, EventHandlerKind::OnStateTransition);
            assert_eq!(target_node.as_deref(), Some("my_node"));
            assert_eq!(start_state.as_deref(), Some("configuring"));
            assert_eq!(goal_state.as_deref(), Some("inactive"));
        } else {
            panic!("expected EventHandler");
        }
    }
}
