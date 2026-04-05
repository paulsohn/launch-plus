"""Marker actions — appear only in the resolved tree, not during parse.

These actions execute to an empty list and serialize to XML comments.
They are inserted by IncludeLaunchDescription to mark source boundaries.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from roscope.entities.action import Action


class SourceMarker(Action):
    """Marks the start of an included file in the resolved tree.

    Serializes to ``<!-- source: pkg://share_path -->``.
    """

    def __init__(self, package: str, share_path: str, include_args: dict | None = None):
        self.package = package
        self.share_path = share_path
        self.include_args = include_args or {}

    def execute(self, context) -> list:
        return []

    def label(self) -> str:
        if self.package:
            return f"{self.package}://{self.share_path}"
        return self.share_path

    def serialize_resolved(self) -> list[ET.Element]:
        return [ET.Comment(f" source: {self.label()} ")]  # type: ignore[list-item]


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
        return [ET.Comment(f' arg name="{self.name}" {attr}="{esc}" ')]  # type: ignore[list-item]
