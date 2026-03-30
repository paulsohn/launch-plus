"""XML launch file parser — produces :class:`XmlEntity` trees.

Adapted from ROS 2 ``launch_xml.entity`` and ``launch_xml.parser``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from launch_plus.parsers.entity import Entity

# ── Type coercion helpers ────────────────────────────────────────────────────

_BOOL_TRUE = frozenset({"true", "1", "yes", "on"})
_BOOL_FALSE = frozenset({"false", "0", "no", "off"})


def _coerce_to_bool(value: str) -> bool:
    lower = value.strip().lower()
    if lower in _BOOL_TRUE:
        return True
    if lower in _BOOL_FALSE:
        return False
    raise ValueError(f"Cannot coerce {value!r} to bool")


def _coerce(value: str, data_type: type) -> Any:
    """Coerce a string *value* to the requested *data_type*."""
    if data_type is str:
        return value
    if data_type is bool:
        return _coerce_to_bool(value)
    if data_type is int:
        return int(value)
    if data_type is float:
        return float(value)
    # Fallback: return as string.
    return value


# ── XmlEntity ────────────────────────────────────────────────────────────────


class XmlEntity(Entity):
    """An :class:`Entity` backed by an :class:`xml.etree.ElementTree.Element`.

    Attributes are read from XML attributes with type coercion.
    Children are child XML elements wrapped as :class:`XmlEntity`.
    """

    def __init__(
        self,
        xml_element: ET.Element,
        *,
        parent: XmlEntity | None = None,
    ) -> None:
        self._xml = xml_element
        self._parent = parent
        self._read_attributes: set[str] = set()
        self._read_children: set[str] = set()

    # ── Entity interface ─────────────────────────────────────────────────

    @property
    def type_name(self) -> str:
        return self._xml.tag

    @property
    def parent(self) -> XmlEntity | None:
        return self._parent

    @property
    def children(self) -> list[XmlEntity]:
        self._read_children = {item.tag for item in self._xml}
        return [XmlEntity(item, parent=self) for item in self._xml]

    def get_attr(
        self,
        name: str,
        *,
        data_type: type = str,
        optional: bool = False,
    ) -> Any:
        # ── List[Entity]: return child elements matching *name* ──────
        if data_type is list:
            matched = [x for x in self._xml if x.tag == name]
            if not matched:
                if optional:
                    return None
                raise AttributeError(f"No child element <{name}> in <{self.type_name}>")
            self._read_children.add(name)
            return [XmlEntity(item, parent=self) for item in matched]

        # ── Scalar attribute ─────────────────────────────────────────
        value = self._xml.attrib.get(name)
        if value is None:
            if optional:
                return None
            raise AttributeError(f"Attribute '{name}' not found in <{self.type_name}>")
        self._read_attributes.add(name)
        return _coerce(value, data_type)

    def assert_entity_completely_parsed(self) -> None:
        unparsed_attrs = set(self._xml.attrib.keys()) - self._read_attributes
        unparsed_children = {item.tag for item in self._xml} - self._read_children
        errors: list[str] = []
        if unparsed_attrs:
            errors.append(f"unexpected attributes in <{self.type_name}>: {unparsed_attrs}")
        if unparsed_children:
            errors.append(f"unexpected child elements in <{self.type_name}>: {unparsed_children}")
        if errors:
            raise ValueError("; ".join(errors))

    # ── Convenience ──────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"XmlEntity(<{self.type_name}> attrs={dict(self._xml.attrib)})"


# ── Parser entry point ───────────────────────────────────────────────────────


def parse_xml_launch(content: str, file_path: str) -> list[XmlEntity]:
    """Parse an XML launch file and return a list of root-level :class:`XmlEntity` objects."""
    root = ET.fromstring(content)  # noqa: S314
    if root.tag != "launch":
        from launch_plus.resolver import _state

        _state.warn(f"XML launch file '{file_path}' has unexpected root tag <{root.tag}>")
    return [XmlEntity(child) for child in root]
