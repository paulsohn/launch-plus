//! Parsers for ROS 2 launch file formats.
//!
//! Both the XML and YAML parsers produce a common [`RawElement`] tree (tag + attributes +
//! children), which is then converted into the typed [`LaunchElement`] AST by
//! [`raw_to_launch_elements`].
//!
//! - [`xml`]: Parse `.launch.xml` files → `Vec<RawElement>`.
//! - [`yaml`]: Parse `.launch.yaml` files → `Vec<RawElement>`.

pub mod xml;
pub mod yaml;

use std::collections::HashMap;
use std::path::PathBuf;

// ============================================================================
// AST Types — the typed representation of a parsed launch file
// ============================================================================

/// A parsed launch file (AST)
#[derive(Debug, Clone)]
pub struct LaunchFile {
    /// Path to the launch file
    pub path: PathBuf,
    /// Root elements
    pub elements: Vec<LaunchElement>,
}

/// A launch file element
#[derive(Debug, Clone)]
pub enum LaunchElement {
    /// Argument declaration: `<arg name="..." default="..."/>`
    Arg {
        name: String,
        default: Option<String>,
        description: Option<String>,
    },
    /// Variable assignment: `<let name="..." value="..." [if/unless="..."]/>`.
    ///
    /// Both `if=` and `unless=` are honoured.  The ROS 2 `<let>` idiom
    /// ```xml
    /// <let name="x" value="a" if="$(var flag)"/>
    /// <let name="x" value="b" unless="$(var flag)"/>
    /// ```
    /// requires condition support to produce the correct value.
    Let { name: String, value: String, condition: Option<Condition> },
    /// Group with optional condition: `<group if="...">`
    Group {
        condition: Option<Condition>,
        scoped: bool,
        children: Vec<LaunchElement>,
    },
    /// Include another launch file: `<include file="..."/>`
    Include {
        file: String,
        condition: Option<Condition>,
        args: Vec<IncludeArg>,
    },
    /// Node declaration: `<node pkg="..." exec="..."/>` (`name` is optional in ROS 2)
    Node {
        pkg: String,
        exec: String,
        name: Option<String>,
        namespace: Option<String>,
        condition: Option<Condition>,
        params: Vec<Param>,
        remaps: Vec<Remap>,
        envs: Vec<Env>,
        /// Output destination for stdout/stderr (`output="screen"`, `"log"`, `"both"`).
        output: Option<String>,
        /// Extra command-line arguments passed to the node executable (`args="--foo bar"`).
        args: Option<String>,
        /// Whether ROS 2 should restart the node process if it exits (`respawn="true/false"`).
        respawn: Option<String>,
        /// Seconds to wait before restarting (`respawn_delay="1.0"`).
        respawn_delay: Option<String>,
        /// Attribute names present in the source XML that the resolver does not recognise.
        /// Reported as errors during resolution so the user knows they are dropped.
        unknown_attrs: Vec<String>,
    },
    /// Set environment variable: `<set_env name="..." value="..."/>`
    SetEnv { name: String, value: String, condition: Option<Condition> },
    /// Unset environment variable: `<unset_env name="..."/>`
    UnsetEnv { name: String, condition: Option<Condition> },
    /// Namespace push: `<push-ros-namespace namespace="..."/>`
    ///
    /// Prepends a namespace component to all nodes resolved within the enclosing
    /// `<group>`.  The namespace is restored when the group exits.
    PushRosNamespace {
        namespace: String,
        condition: Option<Condition>,
    },
    /// Composable node container: `<node_container pkg="..." exec="..." name="...">`
    ///
    /// Resolves to a node for the container process itself plus one resolved node per
    /// `<composable_node>` child (with `plugin` as the executable identifier).
    NodeContainer {
        pkg: String,
        exec: String,
        name: Option<String>,
        namespace: Option<String>,
        condition: Option<Condition>,
        composable_nodes: Vec<ComposableNode>,
    },
    /// Load composable nodes into a running container: `<load_composable_node target="...">`
    ///
    /// Does not emit a container node; emits one resolved node per `<composable_node>` child.
    LoadComposableNode {
        target: Option<String>,
        namespace: Option<String>,
        condition: Option<Condition>,
        composable_nodes: Vec<ComposableNode>,
    },
    /// Set a global parameter: `<set_parameter name="..." value="..."/>`
    SetParameter { name: String, value: String },
    /// Set a global topic remap: `<set_remap from="..." to="..."/>`
    SetRemap { from: String, to: String },
    /// Log message: `<log message="..."/>`. Emitted as-is in resolved XML.
    Log { message: String },
    /// An XML element whose tag name is not recognised by the parser.
    ///
    /// Surfaced as a warning in the resolve result so the user knows something may
    /// have been silently skipped.  The element's children are already skipped by
    /// the parser before this variant is produced.
    UnknownElement { tag_name: String },
    /// External process: `<executable cmd="..." name="..." shell="true/false"/>`.
    ///
    /// Equivalent to Python `ExecuteProcess`.  Rendered as-is in resolved XML.
    Executable {
        cmd: String,
        name: Option<String>,
        shell: bool,
        condition: Option<Condition>,
    },
}

/// Argument passed to an include
#[derive(Debug, Clone)]
pub struct IncludeArg {
    pub name: String,
    pub value: String,
}

/// Parameter for a node
#[derive(Debug, Clone)]
pub struct Param {
    /// Parameter name. Optional when `from` is set (`<param from="..."/>`).
    pub name: Option<String>,
    pub value: Option<String>,
    pub from: Option<String>,
}

/// Topic remapping
#[derive(Debug, Clone)]
pub struct Remap {
    pub from: String,
    pub to: String,
}

/// Environment variable for a node
#[derive(Debug, Clone)]
pub struct Env {
    pub name: String,
    pub value: String,
}

/// A composable node plugin loaded into a container process.
///
/// The `plugin` field (C++ class name, e.g. `nebula::ros::HesaiRosWrapper`) serves
/// as the executable identifier, mirroring the Python resolver's treatment of composable nodes.
#[derive(Debug, Clone)]
pub struct ComposableNode {
    pub pkg: String,
    pub plugin: String,
    pub name: Option<String>,
    pub namespace: Option<String>,
    pub condition: Option<Condition>,
    pub params: Vec<Param>,
    pub remaps: Vec<Remap>,
}

/// Conditional expression (if/unless)
#[derive(Debug, Clone)]
pub struct Condition {
    pub kind: ConditionKind,
    pub expr: String,
}

/// Type of condition
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConditionKind {
    If,
    Unless,
}

// ---------------------------------------------------------------------------
// RawElement — format-agnostic intermediate representation
// ---------------------------------------------------------------------------

/// A format-agnostic representation of a single launch file element.
///
/// Both the XML and YAML parsers emit `RawElement` trees.  The shared
/// [`raw_to_launch`] function converts them into the typed [`LaunchElement`]
/// AST, validating required attributes and collecting unknown attributes in
/// a single place.
#[derive(Debug, Clone)]
pub struct RawElement {
    pub tag: String,
    pub attrs: HashMap<String, String>,
    pub children: Vec<RawElement>,
}

impl RawElement {
    /// Get a required attribute or return a parse error.
    fn require(&self, name: &str) -> crate::Result<String> {
        self.attrs.get(name).cloned().ok_or_else(|| {
            crate::Error::LaunchParse(format!(
                "missing required attribute '{}' on <{}>",
                name, self.tag
            ))
        })
    }

    /// Get an optional attribute.
    fn get(&self, name: &str) -> Option<String> {
        self.attrs.get(name).cloned()
    }

    /// Extract an `if=` or `unless=` condition from attrs.
    fn condition(&self) -> crate::Result<Option<Condition>> {
        if let Some(expr) = self.get("if") {
            Ok(Some(Condition {
                kind: ConditionKind::If,
                expr,
            }))
        } else if let Some(expr) = self.get("unless") {
            Ok(Some(Condition {
                kind: ConditionKind::Unless,
                expr,
            }))
        } else {
            Ok(None)
        }
    }

    /// Return attribute names that are not in the `known` set.
    fn unknown_attrs(&self, known: &[&str]) -> Vec<String> {
        self.attrs
            .keys()
            .filter(|k| !known.contains(&k.as_str()))
            .cloned()
            .collect()
    }

    /// Return children whose tag matches `tag`.
    fn children_by_tag(&self, tag: &str) -> Vec<&RawElement> {
        self.children.iter().filter(|c| c.tag == tag).collect()
    }
}

// ---------------------------------------------------------------------------
// RawElement → LaunchElement conversion (shared by XML + YAML)
// ---------------------------------------------------------------------------

/// Convert a list of `RawElement`s into typed `LaunchElement`s.
pub fn raw_to_launch_elements(raws: Vec<RawElement>) -> crate::Result<Vec<LaunchElement>> {
    raws.into_iter()
        .filter_map(|raw| raw_to_launch(raw).transpose())
        .collect()
}

/// Convert a single `RawElement` into a typed `LaunchElement`.
fn raw_to_launch(raw: RawElement) -> crate::Result<Option<LaunchElement>> {
    match raw.tag.as_str() {
        "arg" => {
            let name = raw.require("name")?;
            let default = raw.get("default");
            let description = raw.get("description");
            Ok(Some(LaunchElement::Arg {
                name,
                default,
                description,
            }))
        }
        "let" => {
            let name = raw.require("name")?;
            let value = raw.require("value")?;
            let condition = raw.condition()?;
            Ok(Some(LaunchElement::Let {
                name,
                value,
                condition,
            }))
        }
        "group" => {
            let condition = raw.condition()?;
            let scoped = raw
                .get("scoped")
                .map(|v| v == "true")
                .unwrap_or(true);
            let children = raw_to_launch_elements(raw.children)?;
            Ok(Some(LaunchElement::Group {
                condition,
                scoped,
                children,
            }))
        }
        "include" => {
            let file = raw.require("file")?;
            let condition = raw.condition()?;
            let args = raw
                .children_by_tag("arg")
                .into_iter()
                .filter_map(|a| {
                    let name = a.get("name")?;
                    let value = a.get("value")?;
                    Some(IncludeArg { name, value })
                })
                .collect();
            Ok(Some(LaunchElement::Include {
                file,
                condition,
                args,
            }))
        }
        "node" => {
            let pkg = raw.require("pkg")?;
            let exec = raw.require("exec")?;
            let name = raw.get("name");
            let namespace = raw.get("namespace");
            let output = raw.get("output");
            let args = raw.get("args");
            let respawn = raw.get("respawn");
            let respawn_delay = raw.get("respawn_delay");
            let condition = raw.condition()?;
            let unknown_attrs = raw.unknown_attrs(&[
                "pkg", "exec", "name", "namespace", "if", "unless",
                "output", "args", "respawn", "respawn_delay",
            ]);
            let (params, remaps, envs) = extract_node_children(&raw.children)?;
            Ok(Some(LaunchElement::Node {
                pkg,
                exec,
                name,
                namespace,
                condition,
                params,
                remaps,
                envs,
                output,
                args,
                respawn,
                respawn_delay,
                unknown_attrs,
            }))
        }
        "node_container" | "composable_node_container" => {
            let pkg = raw.get("pkg").or_else(|| raw.get("package")).unwrap_or_default();
            let exec = raw.get("exec").or_else(|| raw.get("executable")).unwrap_or_default();
            if pkg.is_empty() {
                return Err(crate::Error::LaunchParse(format!(
                    "missing required attribute 'pkg' on <{}>",
                    raw.tag
                )));
            }
            if exec.is_empty() {
                return Err(crate::Error::LaunchParse(format!(
                    "missing required attribute 'exec' on <{}>",
                    raw.tag
                )));
            }
            let name = raw.get("name");
            let namespace = raw.get("namespace");
            let condition = raw.condition()?;
            let composable_nodes = extract_composable_nodes(&raw.children)?;
            Ok(Some(LaunchElement::NodeContainer {
                pkg,
                exec,
                name,
                namespace,
                condition,
                composable_nodes,
            }))
        }
        "load_composable_node" => {
            let target = raw.get("target");
            let namespace = raw.get("namespace");
            let condition = raw.condition()?;
            let composable_nodes = extract_composable_nodes(&raw.children)?;
            Ok(Some(LaunchElement::LoadComposableNode {
                target,
                namespace,
                condition,
                composable_nodes,
            }))
        }
        "set_env" => {
            let name = raw.require("name")?;
            let value = raw.require("value")?;
            let condition = raw.condition()?;
            Ok(Some(LaunchElement::SetEnv {
                name,
                value,
                condition,
            }))
        }
        "unset_env" => {
            let name = raw.require("name")?;
            let condition = raw.condition()?;
            Ok(Some(LaunchElement::UnsetEnv { name, condition }))
        }
        "push-ros-namespace" => {
            let namespace = raw.require("namespace")?;
            let condition = raw.condition()?;
            Ok(Some(LaunchElement::PushRosNamespace {
                namespace,
                condition,
            }))
        }
        "set_parameter" => {
            let name = raw.require("name")?;
            let value = raw.require("value")?;
            Ok(Some(LaunchElement::SetParameter { name, value }))
        }
        "set_remap" => {
            let from = raw.require("from")?;
            let to = raw.require("to")?;
            Ok(Some(LaunchElement::SetRemap { from, to }))
        }
        "log" => {
            let message = raw.get("message").unwrap_or_default();
            Ok(Some(LaunchElement::Log { message }))
        }
        "executable" => {
            let cmd = raw.get("cmd").unwrap_or_default();
            let name = raw.get("name");
            let shell = raw.get("shell").map(|s| s == "true").unwrap_or(false);
            let condition = raw.condition()?;
            Ok(Some(LaunchElement::Executable {
                cmd,
                name,
                shell,
                condition,
            }))
        }
        _ => Ok(Some(LaunchElement::UnknownElement {
            tag_name: raw.tag,
        })),
    }
}

// ---------------------------------------------------------------------------
// Child element extractors (shared by node, node_container, etc.)
// ---------------------------------------------------------------------------

/// Extract `<param>`, `<remap>`, `<env>` children from a raw element's child list.
fn extract_node_children(
    children: &[RawElement],
) -> crate::Result<(Vec<Param>, Vec<Remap>, Vec<Env>)> {
    let mut params = Vec::new();
    let mut remaps = Vec::new();
    let mut envs = Vec::new();

    for child in children {
        match child.tag.as_str() {
            "param" => {
                let name = child.get("name");
                let value = child.get("value");
                let from = child.get("from");
                match (&name, &value, &from) {
                    (Some(_), Some(_), None) | (None, None, Some(_)) => {}
                    _ => {
                        return Err(crate::Error::LaunchParse(
                            "<param> requires either 'name'+'value' or 'from'".to_string(),
                        ));
                    }
                }
                params.push(Param { name, value, from });
            }
            "remap" => {
                let from = child.require("from")?;
                let to = child.require("to")?;
                remaps.push(Remap { from, to });
            }
            "env" => {
                let name = child.require("name")?;
                let value = child.require("value")?;
                envs.push(Env { name, value });
            }
            _ => {
                // Ignore unknown children inside nodes (e.g. <choice>, comments).
            }
        }
    }

    Ok((params, remaps, envs))
}

/// Extract `<composable_node>` children from a raw element's child list.
fn extract_composable_nodes(
    children: &[RawElement],
) -> crate::Result<Vec<ComposableNode>> {
    let mut nodes = Vec::new();

    for child in children {
        if child.tag != "composable_node" {
            continue;
        }
        let pkg = child.require("pkg")?;
        let plugin = child.require("plugin")?;
        let name = child.get("name");
        let namespace = child.get("namespace");
        let condition = child.condition()?;
        let (params, remaps, _) = extract_node_children(&child.children)?;
        nodes.push(ComposableNode {
            pkg,
            plugin,
            name,
            namespace,
            condition,
            params,
            remaps,
        });
    }

    Ok(nodes)
}
