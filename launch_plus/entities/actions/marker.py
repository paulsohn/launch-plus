"""Marker actions — appear only in the resolved tree, not during parse.

These actions execute to an empty list and serialize to XML comments.
They are inserted by IncludeLaunchDescription to mark source boundaries.
"""

from __future__ import annotations

from launch_plus.entities.action import Action


class SourceMarker(Action):
    """Marks the start of an included file in the resolved tree.

    Serializes to ``<!-- source: pkg://share_path -->`` followed by ``<group>``.
    """

    def __init__(self, package: str, share_path: str, include_args: dict | None = None):
        self.package = package
        self.share_path = share_path
        self.include_args = include_args or {}

    def execute(self, context) -> list:
        return []

    def serialize_resolved(self, indent: str = "  ") -> str:
        label = f"{self.package}://{self.share_path}" if self.package else self.share_path
        out = f"{indent}<!-- source: {label} -->\n"
        out += f"{indent}<group>\n"
        # Render arg comments inside the group
        for name in sorted(self.include_args):
            value = self.include_args[name]
            out += f'{indent}  <!-- arg name="{self._esc(name)}" value="{self._esc(value)}" -->\n'
        return out


class EndSourceMarker(Action):
    """Marks the end of an included file in the resolved tree.

    Serializes to ``</group>`` followed by ``<!-- end: pkg://share_path -->``.
    """

    def __init__(self, package: str, share_path: str):
        self.package = package
        self.share_path = share_path

    def execute(self, context) -> list:
        return []

    def serialize_resolved(self, indent: str = "  ") -> str:
        label = f"{self.package}://{self.share_path}" if self.package else self.share_path
        return f"{indent}</group>\n{indent}<!-- end: {label} -->\n"


class ArgComment(Action):
    """Renders as ``<!-- arg name="..." value/default="..." -->`` comment."""

    def __init__(self, name: str, value: str, *, is_default: bool = False):
        self.name = name
        self.value = value
        self.is_default = is_default

    def execute(self, context) -> list:
        return []

    def serialize_resolved(self, indent: str = "  ") -> str:
        attr = "default" if self.is_default else "value"
        name = self._esc(self.name)
        value = self._esc(self.value)
        return f'{indent}<!-- arg name="{name}" {attr}="{value}" -->\n'
