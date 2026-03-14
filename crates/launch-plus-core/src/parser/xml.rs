//! Parser for ROS 2 XML launch files (`.launch.xml`).
//!
//! Converts raw XML into a tree of [`RawElement`]s, then delegates to the
//! shared [`raw_to_launch_elements`] conversion in the parent module.

use std::collections::HashMap;
use std::path::Path;

use quick_xml::Reader;
use quick_xml::events::{BytesStart, Event};

use super::{LaunchFile, RawElement, raw_to_launch_elements};

/// Parse a launch.xml file into an AST.
pub fn parse_launch_xml(content: &str, path: &Path) -> crate::Result<LaunchFile> {
    let mut reader = Reader::from_str(content);
    reader.config_mut().trim_text(true);

    // Find the <launch> root element first
    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) => {
                let tag_name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                if tag_name == "launch" {
                    let raws = parse_raw_children(&mut reader, "launch")?;
                    let elements = raw_to_launch_elements(raws)?;
                    return Ok(LaunchFile {
                        path: path.to_path_buf(),
                        elements,
                    });
                }
            }
            Ok(Event::Eof) => {
                return Err(crate::Error::LaunchParse(
                    "no <launch> root element found".to_string(),
                ));
            }
            Ok(_) => {}
            Err(e) => {
                return Err(crate::Error::LaunchParse(format!("XML parse error: {}", e)));
            }
        }
        buf.clear();
    }
}

// ---------------------------------------------------------------------------
// Generic XML → RawElement tree builder
// ---------------------------------------------------------------------------

/// Collect all attributes from a `BytesStart` into a `HashMap`.
fn collect_attrs(start: &BytesStart) -> HashMap<String, String> {
    start
        .attributes()
        .filter_map(|a| a.ok())
        .filter_map(|a| {
            let key = String::from_utf8_lossy(a.key.as_ref()).into_owned();
            let val = a.unescape_value().ok().map(|v| v.into_owned())?;
            Some((key, val))
        })
        .collect()
}

/// Parse child elements until the matching `</end_tag>` close, returning them
/// as a flat list of [`RawElement`]s.
fn parse_raw_children(reader: &mut Reader<&[u8]>, end_tag: &str) -> crate::Result<Vec<RawElement>> {
    let mut elements = Vec::new();
    let mut buf = Vec::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) => {
                let tag = String::from_utf8_lossy(e.name().as_ref()).to_string();
                let attrs = collect_attrs(e);
                let children = parse_raw_children(reader, &tag)?;
                elements.push(RawElement {
                    tag,
                    attrs,
                    children,
                });
            }
            Ok(Event::Empty(ref e)) => {
                let tag = String::from_utf8_lossy(e.name().as_ref()).to_string();
                let attrs = collect_attrs(e);
                elements.push(RawElement {
                    tag,
                    attrs,
                    children: vec![],
                });
            }
            Ok(Event::End(ref e)) => {
                let tag = String::from_utf8_lossy(e.name().as_ref()).to_string();
                if tag == end_tag {
                    break;
                }
            }
            Ok(Event::Eof) => break,
            Ok(_) => {} // Skip text, comments, processing instructions, etc.
            Err(e) => {
                return Err(crate::Error::LaunchParse(format!(
                    "XML parse error at position {}: {}",
                    reader.buffer_position(),
                    e
                )));
            }
        }
        buf.clear();
    }

    Ok(elements)
}
