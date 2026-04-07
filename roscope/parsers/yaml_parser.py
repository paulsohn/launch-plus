"""YAML launch file parser — produces :class:`YamlEntity` trees.

Adapted from ROS 2 ``launch_yaml.entity`` and ``launch_yaml.parser``.
"""

from __future__ import annotations

import logging
from typing import Any

from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")

# ── Tag normalization ────────────────────────────────────────────────────────

_TAG_ALIASES: dict[str, str] = {
    "push_ros_namespace": "push-ros-namespace",
    "composable_node_container": "node_container",
}


def _normalize_tag(tag: str) -> str:
    """Normalize YAML tag names to match XML conventions."""
    return _TAG_ALIASES.get(tag, tag)


# ── YamlEntity ───────────────────────────────────────────────────────────────


class YamlEntity(Entity):
    """An :class:`Entity` backed by a ``dict`` parsed from YAML.

    Unlike :class:`XmlEntity`, YAML values are natively typed — no
    coercion is needed, only type checking.  Attributes are dict keys;
    children come from a ``children`` key or from list-valued keys.
    """

    def __init__(
        self,
        element: dict[str, Any],
        type_name: str,
        *,
        parent: YamlEntity | None = None,
    ) -> None:
        self._element = element
        self._type_name = _normalize_tag(type_name)
        self._parent = parent
        self._read_keys: set[str] = set()
        self._children_accessed = False

    # ── Entity interface ─────────────────────────────────────────────────

    @property
    def type_name(self) -> str:
        return self._type_name

    @property
    def parent(self) -> YamlEntity | None:
        return self._parent

    @property
    def children(self) -> list[YamlEntity]:
        """Get the Entity's children."""
        self._children_accessed = True
        if not isinstance(self._element, (dict, list)):
            raise TypeError(
                f"Expected a dict or list, got {type(self._element)}:\n---\n{self._element}\n---"
            )
        if isinstance(self._element, dict):
            if "children" not in self._element:
                raise ValueError(
                    f"Expected entity `{self._type_name}` to have children entities."
                    f"That can be a list of subentities or a dictionary with a `children` "
                    "list element"
                )
            self._read_keys.add("children")
            children = self._element["children"]
        else:
            children = self._element
        entities: list[YamlEntity] = []
        for child in children:
            if len(child) != 1:
                raise RuntimeError(
                    "Subentities must be a dictionary with only one key, which is the entity type"
                )
            type_name = list(child.keys())[0]
            entities.append(YamlEntity(child[type_name], type_name, parent=self))
        return entities

    def get_attr(
        self,
        name: str,
        *,
        data_type: type = str,
        optional: bool = False,
    ) -> Any:
        # ── List[Entity]: return child entities from a list-valued key ──
        if data_type is list:
            if name not in self._element:
                if optional:
                    return None
                raise AttributeError(f"No key '{name}' in {self._type_name} entity")
            self._read_keys.add(name)
            raw = self._element[name]
            if not isinstance(raw, list):
                raise TypeError(
                    f"Key '{name}' in {self._type_name} expected to be a list, "
                    f"got {type(raw).__name__}"
                )
            # Each list item is a dict representing a child entity of type *name*.
            return [YamlEntity(item, name, parent=self) for item in raw if isinstance(item, dict)]

        # ── Scalar attribute ─────────────────────────────────────────
        if name not in self._element:
            if optional:
                return None
            raise AttributeError(f"Key '{name}' not found in {self._type_name} entity")
        self._read_keys.add(name)
        value = self._element[name]

        # YAML natively types values — coerce to string when data_type is str
        # (matching XML behaviour where all attributes are strings).
        if data_type is str and value is not None:
            return str(value)
        return value

    def assert_entity_completely_parsed(self) -> None:
        if isinstance(self._element, list):
            if not self._children_accessed:
                raise ValueError(
                    f"Unexpected nested entity(ies) found in `{self._type_name}`: {self._element}"
                )
            return
        unparsed_keys = set(self._element.keys()) - self._read_keys
        if unparsed_keys:
            raise ValueError(f"Unexpected key(s) found in `{self._type_name}`: {unparsed_keys}")

    # ── Convenience ──────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"YamlEntity({self._type_name!r}, keys={list(self._element.keys())})"


# ── Parser entry point ───────────────────────────────────────────────────────


def parse_yaml_launch(content: str, file_path: str) -> list[YamlEntity]:
    """Parse a YAML launch file and return a list of root-level :class:`YamlEntity` objects.

    Expects::

        launch:
          - arg:
              name: my_arg
              default: value
          - node:
              pkg: my_pkg
              ...
    """
    import yaml

    data = yaml.safe_load(content)
    if not isinstance(data, dict) or "launch" not in data:
        logger.warning("YAML launch file '%s' missing 'launch' root key", file_path)
        return []

    launch_list = data["launch"]
    if not isinstance(launch_list, list):
        logger.warning("YAML 'launch' key in '%s' is not a list", file_path)
        return []

    entities: list[YamlEntity] = []
    for entry in launch_list:
        if not isinstance(entry, dict) or len(entry) != 1:
            continue
        tag = next(iter(entry))
        attrs = entry[tag]
        if not isinstance(attrs, dict):
            continue
        entities.append(YamlEntity(attrs, tag))

    return entities
