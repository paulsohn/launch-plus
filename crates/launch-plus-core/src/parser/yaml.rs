//! Parsers for YAML launch files and ROS 2 parameter YAML files.
//!
//! Converts YAML into a tree of [`RawElement`]s, then delegates to the
//! shared [`raw_to_launch_elements`] conversion in the parent module.

use std::collections::HashMap;

use super::{LaunchFile, RawElement, raw_to_launch_elements};

/// Parse a ROS 2 YAML launch file into the same `LaunchFile` AST as XML parsing.
///
/// The ROS 2 YAML launch format mirrors the XML format element-for-element:
///
/// ```yaml
/// launch:
///   - arg:
///       name: vehicle_model
///       default: sample_vehicle
///   - let:
///       name: config_dir
///       value: "$(find-pkg-share my_pkg)/config"
///   - push-ros-namespace:
///       namespace: "$(var ns)"
///       if: "$(var use_ns)"
///   - group:
///       scoped: false
///       if: "$(var condition)"
///       children:
///         - arg:
///             name: nested_arg
///             default: val
///   - include:
///       file: "$(find-pkg-share pkg)/launch/file.launch.xml"
///       arg:
///         - name: vehicle_model
///           value: "$(var vehicle_model)"
///   - node:
///       pkg: my_pkg
///       exec: my_node
///       name: my_name
///       namespace: /my_ns
///       if: "$(var condition)"
///       param:
///         - name: foo
///           value: bar
///       remap:
///         - from: /in
///           to: /out
/// ```
///
/// YAML launch files included from XML are resolved inline in the enclosing scope so that
/// their `<arg>` defaults and `<let>` assignments become visible to subsequent siblings.
pub fn parse_launch_yaml(content: &str, path: &std::path::Path) -> crate::Result<LaunchFile> {
    let yaml: serde_yaml::Value = serde_yaml::from_str(content).map_err(|e| {
        crate::Error::LaunchParse(format!("YAML parse error in '{}': {e}", path.display()))
    })?;

    let entries = yaml
        .get("launch")
        .and_then(|v| v.as_sequence())
        .ok_or_else(|| {
            crate::Error::LaunchParse(format!(
                "YAML launch file '{}' is missing a top-level 'launch:' sequence",
                path.display()
            ))
        })?;

    let raws = yaml_sequence_to_raws(entries);
    let elements = raw_to_launch_elements(raws)?;
    Ok(LaunchFile {
        path: path.to_path_buf(),
        elements,
    })
}

// ---------------------------------------------------------------------------
// YAML → RawElement tree builder
// ---------------------------------------------------------------------------

/// Convert a YAML sequence of entries (each a single-key mapping like
/// `{node: {pkg: ..., exec: ...}}`) into a list of `RawElement`.
fn yaml_sequence_to_raws(entries: &[serde_yaml::Value]) -> Vec<RawElement> {
    entries.iter().filter_map(yaml_entry_to_raw).collect()
}

/// Convert one YAML entry into a `RawElement`.
///
/// Each entry is a single-key mapping.  The key is the element tag;
/// the value is a mapping whose scalar keys become `attrs` and whose
/// sequence keys become synthesized `children`.
fn yaml_entry_to_raw(entry: &serde_yaml::Value) -> Option<RawElement> {
    let mapping = entry.as_mapping()?;
    let (raw_tag_value, body) = mapping.into_iter().next()?;
    let raw_tag = raw_tag_value.as_str()?;

    // Normalise YAML-specific aliases to match the XML tag names used by
    // the shared raw_to_launch conversion.
    let tag = match raw_tag {
        "push_ros_namespace" => "push-ros-namespace",
        "composable_node_container" => "node_container",
        other => other,
    };

    let body_mapping = body.as_mapping()?;
    Some(yaml_mapping_to_raw(tag, body_mapping))
}

/// Build a `RawElement` from a tag name and a YAML mapping body.
///
/// - Scalar-valued keys → `attrs`
/// - The special `children` key (used by `<group>`) → recurse each entry
/// - Other sequence-valued keys → each list item becomes a child `RawElement`
///   with `tag = key`, whose mapping entries are processed recursively
fn yaml_mapping_to_raw(tag: &str, mapping: &serde_yaml::Mapping) -> RawElement {
    let mut attrs = HashMap::new();
    let mut children = Vec::new();

    for (k, v) in mapping {
        let key = match k.as_str() {
            Some(s) => s,
            None => continue,
        };

        match v {
            // Scalar values → attrs
            serde_yaml::Value::String(s) => {
                attrs.insert(key.to_string(), s.clone());
            }
            serde_yaml::Value::Bool(b) => {
                attrs.insert(key.to_string(), b.to_string());
            }
            serde_yaml::Value::Number(n) => {
                attrs.insert(key.to_string(), n.to_string());
            }

            // Sequence values → child elements
            serde_yaml::Value::Sequence(seq) => {
                if key == "children" {
                    // `children:` in <group> → recurse as top-level entries
                    children.extend(yaml_sequence_to_raws(seq));
                } else {
                    // param, remap, env, arg, composable_node, etc.
                    for item in seq {
                        if let Some(m) = item.as_mapping() {
                            children.push(yaml_mapping_to_raw(key, m));
                        }
                    }
                }
            }

            // Null → skip
            serde_yaml::Value::Null => {}

            // Other (mapping, tagged) → try to coerce to string for attrs
            other => {
                if let Ok(s) = serde_yaml::to_string(other) {
                    attrs.insert(key.to_string(), s.trim().to_string());
                }
            }
        }
    }

    RawElement {
        tag: tag.to_string(),
        attrs,
        children,
    }
}
