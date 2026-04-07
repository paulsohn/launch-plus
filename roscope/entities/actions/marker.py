"""Marker actions — appear only in the resolved tree, not during parse.

These actions execute to an empty list and serialize to XML comments.
They are inserted by IncludeLaunchDescription to mark source boundaries.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from roscope.entities.action import Action
from roscope.entities.helpers import sanitize_xml_comment


class SourceMarker(Action):
    """Marks the start of an included file in the resolved tree.

    Serializes to ``<!-- source: /absolute/path/to/file.launch.xml -->``.
    """

    def __init__(self, file_path: str, include_args: dict | None = None):
        self.file_path = file_path
        self.include_args = include_args or {}

    def execute(self, context) -> list:
        return []

    def serialize_resolved(self) -> list[ET.Element]:
        return [ET.Comment(sanitize_xml_comment(f" source: {self.file_path} "))]  # type: ignore[list-item]


class ArgComment(Action):
    """Renders as ``<!-- arg name="..." value/default="..." -->`` comment."""

    def __init__(self, name: str, value: str, *, is_default: bool = False):
        self.name = name
        self.value = value
        self.is_default = is_default

    def execute(self, context) -> list:
        return []

    def serialize_resolved(self) -> list[ET.Element]:
        attr = "default" if self.is_default else "value"
        esc = escape(self.value, {'"': "&quot;"})
        return [ET.Comment(sanitize_xml_comment(f' arg name="{self.name}" {attr}="{esc}" '))]  # type: ignore[list-item]
