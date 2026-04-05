"""Render resolved launch actions as XML."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)


def render_resolved_xml(
    package: str,
    launcher: str,
    actions: list,
    *,
    show_args: bool = False,
    initial_args: dict[str, str] | None = None,
) -> str:
    """Render resolved actions as a ``<launch>`` XML document.

    Each action's ``serialize_resolved()`` returns ``list[ET.Element]``.
    The renderer simply iterates and appends — no special-case logic.
    """
    root = ET.Element("launch")

    # Top-level initial args as comments (CLI --show-args)
    if show_args and initial_args:
        for name in sorted(initial_args):
            value = initial_args[name]
            esc = escape(value, {'"': "&quot;"})
            root.append(ET.Comment(f' arg name="{name}" value="{esc}" '))

    for action in actions:
        for elem in action.serialize_resolved():
            root.append(elem)

    ET.indent(root, space="  ")
    xml_str = ET.tostring(root, encoding="unicode")
    xml_str = xml_str.replace(" />", "/>")

    header = f"<!-- resolved by roscope from {package}://launch/{launcher} -->\n"
    return header + xml_str
