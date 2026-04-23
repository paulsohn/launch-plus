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
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/load_composable_nodes.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Action handler for <load_composable_node>.

Matching official ``launch_ros.actions.LoadComposableNodes``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.actions.composable_node_container import (
    ComposableNodeContainer,
    _resolve_plugins,
)
from roscope.entities.actions.node import Node, _parse_optional
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _ros2_namespace_join
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity


@expose_action("load_composable_node")
class LoadComposableNodes(Action):
    """Loads composable nodes into an existing container.

    Matching official ``launch_ros.actions.LoadComposableNodes``.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        target_raw = entity.get_attr("target", optional=True)
        ns_raw = entity.get_attr("namespace", optional=True)
        kwargs["target_container"] = _parse_optional(parser, target_raw)
        kwargs["composable_node_descriptions"] = Node.parse_composable_plugins(entity, parser)
        kwargs["namespace"] = _parse_optional(parser, ns_raw)
        return cls, kwargs

    def __init__(self, *, composable_node_descriptions=None, target_container=None, **kwargs):
        super().__init__(**kwargs)
        self.target_container = target_container
        self.namespace = kwargs.get("namespace")
        self.composable_node_descriptions: list = list(composable_node_descriptions or [])
        self.target: str = ""
        self.ros_namespace: str | None = None
        self.explicit_namespace: str | None = None

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved LoadComposableNodes."""
        state = context._state

        for desc in self.composable_node_descriptions:
            raw_pkg = getattr(desc, "package", None)
            if raw_pkg:
                state.track_package(raw_pkg)

        ns = context.perform_substitution(self.namespace) if self.namespace else None
        ros_ns = context._launch_configurations.get("ros_namespace")

        target = ""
        if self.target_container is not None:
            if isinstance(self.target_container, ComposableNodeContainer):
                # Python shim: container stores its FQN (matching official)
                target = getattr(self.target_container, "fqn", "")
            else:
                raw_target = context.perform_substitution(self.target_container) or ""
                # Non-FQN targets resolve under root namespace (matching official:
                # create_client uses the launcher node's namespace which is /)
                if raw_target and not raw_target.startswith("/"):
                    target = "/" + raw_target
                else:
                    target = raw_target

        valid_composable_nodes = [
            desc
            for desc in self.composable_node_descriptions
            if desc.condition() is None or desc.condition().evaluate(context)
        ]
        _resolve_plugins(valid_composable_nodes, context)

        resolved = LoadComposableNodes()
        resolved.target = target
        resolved.ros_namespace = ros_ns
        resolved.explicit_namespace = ns
        resolved.namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved.composable_node_descriptions = valid_composable_nodes
        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.composable_node_descriptions:
            return []
        if not self.target:
            return []

        elem = ET.Element("load_composable_node")
        elem.set("target", self.target)

        for desc in self.composable_node_descriptions:
            for child_elem in desc.serialize_resolved():
                elem.append(child_elem)

        return [elem]
