"""Render resolved launch actions as XML."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from launch_plus.types import IncludeArgContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _xml_escape(s: str) -> str:
    """Escape special XML characters in an attribute value."""
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _add_show_args(
    parent: ET.Element,
    key: tuple[str, Path],
    include_args: dict[tuple[str, Path], IncludeArgContext],
    declared_args_by_file: dict[tuple[str, Path], dict[str, str]],
) -> None:
    """Add ``<!-- arg ... -->`` comments to *parent* for a given include boundary."""
    ctx = include_args.get(key)
    explicit = dict(ctx.explicit) if ctx else {}
    declared = dict(declared_args_by_file.get(key, {}))

    merged: dict[str, tuple[str, bool]] = {}
    for name, value in explicit.items():
        merged[name] = (value, False)
    for name, value in declared.items():
        if name not in merged:
            merged[name] = (value, True)

    for name in sorted(merged):
        value, is_default = merged[name]
        attr = "default" if is_default else "value"
        parent.append(ET.Comment(f' arg name="{name}" {attr}="{_xml_escape(value)}" '))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_resolved_xml(
    package: str,
    launcher: str,
    actions: list,
    *,
    include_args: dict[tuple[str, Path], IncludeArgContext] | None = None,
    show_args: bool = False,
    initial_args: dict[str, str] | None = None,
    declared_args_by_file: dict[tuple[str, Path], dict[str, str]] | None = None,
) -> str:
    """Render resolved actions as a ``<launch>`` XML document.

    Builds an ``xml.etree.ElementTree`` and serializes it to string.
    Source-group nesting is driven by ``SourceMarker`` / ``EndSourceMarker``
    actions in the resolved list.
    """
    from launch_plus.entities.actions.marker import EndSourceMarker, SourceMarker

    if include_args is None:
        include_args = {}
    if initial_args is None:
        initial_args = {}
    if declared_args_by_file is None:
        declared_args_by_file = {}

    root = ET.Element("launch")

    # Initial args as comments
    if show_args and initial_args:
        for name in sorted(initial_args):
            value = initial_args[name]
            root.append(ET.Comment(f' arg name="{name}" value="{_xml_escape(value)}" '))

    # Build tree — SourceMarker/EndSourceMarker drive <group> nesting
    stack: list[ET.Element] = [root]

    for action in actions:
        if isinstance(action, SourceMarker):
            parent = stack[-1]
            parent.append(ET.Comment(f" source: {action.label()} "))
            group = ET.SubElement(parent, "group")
            if show_args:
                key = (action.package, Path(action.share_path))
                _add_show_args(group, key, include_args, declared_args_by_file)
            stack.append(group)
        elif isinstance(action, EndSourceMarker):
            if len(stack) > 1:
                stack.pop()
            stack[-1].append(ET.Comment(f" end: {action.label()} "))
        else:
            for elem in action.serialize_resolved():
                stack[-1].append(elem)

    ET.indent(root, space="  ")
    xml_str = ET.tostring(root, encoding="unicode")
    # Normalize self-closing tag style: <elem /> → <elem/>
    xml_str = xml_str.replace(" />", "/>")

    header = f"<!-- resolved by launch-plus from {package}://launch/{launcher} -->\n"
    return header + xml_str
