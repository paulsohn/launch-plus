# Copyright 2019 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Originally from:
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/descriptions/composable_node.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for a description of a ComposableNode.

Matching official ``launch_ros.descriptions.ComposableNode``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET


class ComposableNode:
    """Describes a ROS node that can be loaded into a container with other nodes."""

    def __init__(
        self,
        *,
        package,
        plugin,
        name=None,
        namespace=None,
        parameters=None,
        remappings=None,
        extra_arguments=None,
        condition=None,
    ) -> None:
        self.__package = package
        self.__node_plugin = plugin

        self.__node_name = name
        self.__node_namespace = namespace

        self.__parameters = list(parameters) if parameters is not None else None
        self.__remappings = list(remappings) if remappings else None
        self.__extra_arguments = list(extra_arguments) if extra_arguments else None

        self.__condition = condition

        # roscope-specific: resolved output data, set by _resolve_plugin()
        self._resolved_data: dict | None = None

    @classmethod
    def parse(cls, parser, entity):
        """Parse composable_node."""
        from roscope.entities.actions.node import Node
        from roscope.entities.parsing import _ParsedCondition

        kwargs: dict = {}

        kwargs["package"] = parser.parse_substitution(entity.get_attr("pkg", optional=True) or "")
        kwargs["plugin"] = parser.parse_substitution(entity.get_attr("plugin", optional=True) or "")
        kwargs["name"] = parser.parse_substitution(entity.get_attr("name", optional=True) or "")

        if_cond = entity.get_attr("if", optional=True)
        unless_cond = entity.get_attr("unless", optional=True)
        if if_cond is not None and unless_cond is not None:
            raise RuntimeError("if and unless conditions can't be used simultaneously")
        if if_cond is not None:
            kwargs["condition"] = _ParsedCondition("If", parser.parse_substitution(if_cond))
        if unless_cond is not None:
            kwargs["condition"] = _ParsedCondition("Unless", parser.parse_substitution(unless_cond))

        namespace = entity.get_attr("namespace", optional=True)
        if namespace is not None:
            kwargs["namespace"] = parser.parse_substitution(namespace)

        parameters = entity.get_attr("param", data_type=list, optional=True)
        if parameters is not None:
            kwargs["parameters"] = Node.parse_params(entity, parser)

        remappings = entity.get_attr("remap", data_type=list, optional=True)
        if remappings is not None:
            kwargs["remappings"] = [
                (
                    parser.parse_substitution(remap.get_attr("from", optional=True) or ""),
                    parser.parse_substitution(remap.get_attr("to", optional=True) or ""),
                )
                for remap in remappings
            ]

        extra_arguments = entity.get_attr("extra_arg", data_type=list, optional=True)
        if extra_arguments is not None:
            kwargs["extra_arguments"] = [
                {
                    tuple(parser.parse_substitution(ea.get_attr("name", optional=True) or "")): (
                        parser.parse_substitution(ea.get_attr("value", optional=True) or "")
                    )
                }
                for ea in extra_arguments
            ]

        return cls, kwargs

    @property
    def package(self):
        """Get node package name."""
        return self.__package

    @property
    def node_plugin(self):
        """Get node plugin name."""
        return self.__node_plugin

    @property
    def node_name(self):
        """Get node name."""
        return self.__node_name

    @property
    def node_namespace(self):
        """Get node namespace."""
        return self.__node_namespace

    @property
    def parameters(self) -> list | None:
        """Get node parameters."""
        return self.__parameters

    @property
    def remappings(self) -> list | None:
        """Get node remapping rules."""
        return self.__remappings

    @property
    def extra_arguments(self) -> list | None:
        """Get container extra arguments."""
        return self.__extra_arguments

    def condition(self):
        """Getter for condition."""
        return self.__condition

    def serialize_resolved(self) -> list[ET.Element]:
        """Render as <composable_node> element (called by parent container)."""
        data = self._resolved_data
        if data is None:
            return []

        elem = ET.Element("composable_node")
        elem.set("pkg", data.get("package", ""))
        elem.set("plugin", data.get("plugin", ""))
        name = data.get("name")
        if name:
            elem.set("name", name)
        ns = data.get("namespace")
        if ns:
            elem.set("namespace", ns)

        from roscope.entities.helpers import _serialize_param_files, _serialize_remaps

        _serialize_param_files(elem, data.get("param_files", []))
        for k, v in data.get("parameters", []):
            p = ET.SubElement(elem, "param")
            p.set("name", k)
            p.set("value", v)
        _serialize_remaps(elem, data.get("remappings", []), data.get("remap_metadata"))
        for ea in data.get("extra_arguments", []):
            e = ET.SubElement(elem, "extra_arg")
            e.set("name", ea.get("name", ""))
            e.set("value", ea.get("value", ""))

        return [elem]

    def __repr__(self):
        return f"ComposableNode(package={self.__package!r}, plugin={self.__node_plugin!r})"
