"""Marker actions — appear only in the resolved tree, not during parse.

These actions execute to an empty list and serialize to XML comments.
They are inserted by IncludeLaunchDescription to mark source boundaries.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from launch_plus.entities.action import Action


class SourceMarker(Action):
    """Marks the start of an included file in the resolved tree.

    The renderer uses this to open a ``<!-- source: ... -->`` comment
    and a ``<group>`` element.  Not serialized via ``serialize_resolved()``
    — the renderer handles the structural nesting directly.
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


class EndSourceMarker(Action):
    """Marks the end of an included file in the resolved tree.

    The renderer uses this to close the ``</group>`` and emit
    ``<!-- end: ... -->`` comment.
    """

    def __init__(self, package: str, share_path: str):
        self.package = package
        self.share_path = share_path

    def execute(self, context) -> list:
        return []

    def label(self) -> str:
        if self.package:
            return f"{self.package}://{self.share_path}"
        return self.share_path


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
        return [ET.Comment(f' arg name="{self.name}" {attr}="{self.value}" ')]  # type: ignore[list-item]
