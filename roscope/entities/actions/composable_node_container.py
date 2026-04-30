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
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/composable_node_container.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Action handler for <node_container>.

Matching official ``launch_ros.actions.ComposableNodeContainer``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from roscope.entities.actions.node import Node
from roscope.entities.descriptions import ComposableNode
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _ros2_namespace_join
from roscope.entities.parameter_descriptions import Parameter, ParameterFile
from roscope.entities.parsing import Parser
from roscope.parsers.entity import Entity


def _resolve_plugin(desc_or_dict, context) -> dict:
    """Resolve a single ComposableNode to output dict."""
    state = context._state
    params: list[tuple[str, str]] = []
    active_params: dict[str, str] = {}
    pf_list: list[dict] = []
    remaps: list = []

    def _push_param(k: str, v: str) -> None:
        if active_params.get(k) != v:
            params.append((k, v))
            active_params[k] = v

    if isinstance(desc_or_dict, ComposableNode):
        desc = desc_or_dict
        pkg = context.perform_substitution(desc.package) or ""
        plugin_name = context.perform_substitution(desc.node_plugin) or ""
        name = context.perform_substitution(desc.node_name) or None
        ros_ns = context._launch_configurations.get("ros_namespace")
        node_ns = None
        if desc.node_namespace is not None:
            node_ns = context.perform_substitution(desc.node_namespace)
        full_ns = _ros2_namespace_join(ros_ns, node_ns) if node_ns else ros_ns
        for p in desc.parameters or []:
            if isinstance(p, ParameterFile):
                path_obj, expanded = p.evaluate(context)
                path = str(path_obj)
                state.track_param_file(path)
                pf_list.append({"path": path, "params": expanded})
                for k, v in expanded:
                    active_params[k] = str(v)
            elif isinstance(p, Parameter):
                k, v = p.evaluate(context)
                _push_param(k, v)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    _push_param(str(k), resolved_v if resolved_v is not None else "")
        for r in desc.remappings or []:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                src = src if src is not None else str(r[0])
                dst = dst if dst is not None else str(r[1])
                if dst and full_ns and not dst.startswith("/") and not dst.startswith("~/"):
                    dst = f"{full_ns.rstrip('/')}/{dst}"
                remaps.append([src, dst])
        extra_args: list[dict] = []
        for ea in desc.extra_arguments or []:
            if isinstance(ea, dict):
                for k_tokens, v_tokens in ea.items():
                    k = context.perform_substitution(list(k_tokens)) or ""
                    v = context.perform_substitution(v_tokens) or ""
                    extra_args.append({"name": k, "value": v})
    else:
        pkg = str(getattr(desc_or_dict, "package", "") or "")
        plugin_name = str(getattr(desc_or_dict, "node_plugin", "") or "")
        name = None
        full_ns = None
        extra_args = []

    if pkg:
        state.track_package(pkg)

    remap_metadata = state.apply_connection_plugin(
        pkg, dict(active_params), remaps, plugin_name=plugin_name
    )

    entry: dict = {
        "package": pkg,
        "plugin": plugin_name,
        "name": name,
        "namespace": full_ns,
        "parameters": params,
        "remappings": remaps,
        "remap_metadata": remap_metadata,
    }
    if pf_list:
        entry["param_files"] = pf_list
    if extra_args:
        entry["extra_arguments"] = extra_args
    if isinstance(desc_or_dict, ComposableNode):
        desc_or_dict._resolved_data = entry
    return entry


def _resolve_plugins(items, context) -> list[dict]:
    """Resolve a list of ComposableNode descriptions to output dicts."""
    return [_resolve_plugin(item, context) for item in (items or [])]


@expose_action("node_container")
class ComposableNodeContainer(Node):
    """A composable node container process.

    Matching official ``launch_ros.actions.ComposableNodeContainer``.
    """

    _tag_name = "node_container"

    @classmethod
    def parse(cls, entity: Entity, parser: Parser, ignore: list | None = None):
        _, kwargs = super().parse(entity, parser)
        kwargs["kind"] = "container"
        kwargs["composable_node_descriptions"] = Node.parse_composable_plugins(entity, parser)
        return cls, kwargs

    def __init__(self, *, composable_node_descriptions=None, **kwargs):
        kwargs.setdefault("kind", "container")
        super().__init__(**kwargs)
        self.composable_node_descriptions: list = list(composable_node_descriptions or [])

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved Container."""
        for desc in self.composable_node_descriptions:
            raw_pkg = getattr(desc, "package", None)
            if raw_pkg:
                context._state.track_package(raw_pkg)

        valid_composable_nodes = [
            desc
            for desc in self.composable_node_descriptions
            if desc.condition() is None or desc.condition().evaluate(context)
        ]
        _resolve_plugins(valid_composable_nodes, context)

        # Delegate Node-level resolution (pkg, params, remaps, env, etc.) to super().
        [resolved] = super().execute(context)

        # Store FQN on the original object for LoadComposableNodes.
        # Matching official: the container internally stores its fully qualified
        # node name so that load actions in different scopes can reference it.
        self.fqn = _ros2_namespace_join(resolved.namespace, resolved.name) or ""

        resolved.composable_node_descriptions = valid_composable_nodes
        return [resolved]

    def _add_children(self, parent: ET.Element) -> None:
        super()._add_children(parent)
        for desc in self.composable_node_descriptions:
            for child_elem in desc.serialize_resolved():
                parent.append(child_elem)
