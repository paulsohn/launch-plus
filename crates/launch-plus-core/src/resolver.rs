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

/// Kind of event handler element.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventHandlerKind {
    OnProcessStart,
    OnProcessExit,
    OnStateTransition,
    OnShutdown,
}

impl EventHandlerKind {
    /// Returns the XML tag name for this handler kind.
    pub fn tag_name(self) -> &'static str {
        match self {
            Self::OnProcessStart => "on_process_start",
            Self::OnProcessExit => "on_process_exit",
            Self::OnStateTransition => "on_state_transition",
            Self::OnShutdown => "on_shutdown",
        }
    }
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
    /// Per-file declared args for `--show-args`: `(package, share_path)` → `{name: default}`.
    pub declared_args_by_file: HashMap<(String, PathBuf), HashMap<String, String>>,
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
    let is_exec = |n: &&ResolvedNode| {
        !matches!(
            n.kind,
            NodeKind::IncludeMarker
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
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

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
}
