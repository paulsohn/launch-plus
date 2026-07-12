# Copyright 2018 Open Source Robotics Foundation, Inc.
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

# Originally from (planned to split and refactor):
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/node.py
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/lifecycle_node.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Action handler for <node> and <lifecycle_node>.

Matching official ``launch_ros.actions.Node`` and ``LifecycleNode``.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from roscope.entities.actions.execute_process import ExecuteProcess
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _current_file, _ros2_namespace_join
from roscope.entities.parameter_descriptions import Parameter, ParameterFile
from roscope.entities.parsing import Parser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


def _parse_optional(parser: Parser, text: str | None) -> list | None:
    """Parse an optional attribute to tokens, or return None."""
    if text is None:
        return None
    return parser.parse_substitution(text)


@expose_action("node")
class Node(ExecuteProcess):
    """Tracks a ROS node."""

    @staticmethod
    def parse_params(entity: Entity, parser: Parser) -> list:
        """Extract <param> children as unresolved parameter objects."""
        from roscope.entities.parameter_descriptions import Parameter, ParameterFile

        items = entity.get_attr("param", data_type=list, optional=True)
        if not items:
            return []
        result: list = []
        for p in items:
            name = p.get_attr("name", optional=True)
            value = p.get_attr("value", optional=True)
            from_file = p.get_attr("from", optional=True)
            allow_substs_raw = p.get_attr("allow_substs", data_type=bool, optional=True)
            if isinstance(allow_substs_raw, str):
                allow_substs = allow_substs_raw.strip().lower() in {"true", "1", "yes", "on"}
            else:
                allow_substs = bool(allow_substs_raw or False)
            if from_file:
                result.append(
                    ParameterFile(parser.parse_substitution(from_file), allow_substs=allow_substs)
                )
            elif name:
                result.append(
                    Parameter(
                        name=parser.parse_substitution(name),
                        value=parser.parse_substitution(value or ""),
                    )
                )
        return result

    @staticmethod
    def parse_remaps(entity: Entity, parser: Parser) -> list:
        """Extract <remap> children as unresolved token pairs."""
        items = entity.get_attr("remap", data_type=list, optional=True)
        if not items:
            return []
        return [
            (
                parser.parse_substitution(r.get_attr("from", optional=True) or ""),
                parser.parse_substitution(r.get_attr("to", optional=True) or ""),
            )
            for r in items
        ]

    @staticmethod
    def parse_composable_plugins(entity: Entity, parser: Parser) -> list:
        """Extract <composable_node> children as ComposableNode instances."""
        from roscope.entities.descriptions import ComposableNode

        items = entity.get_attr("composable_node", data_type=list, optional=True)
        if not items:
            return []
        plugins = []
        for cn in items:
            composable_node_cls, composable_node_kwargs = ComposableNode.parse(parser, cn)
            plugins.append(composable_node_cls(**composable_node_kwargs))
        return plugins

    @classmethod
    def parse(cls, entity: Entity, parser: Parser, ignore: list | None = None):
        _, kwargs = super().parse(entity, parser, ignore=["cmd"])
        kwargs["package"] = parser.parse_substitution(
            entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
        )
        kwargs["executable"] = parser.parse_substitution(
            entity.get_attr("exec", optional=True)
            or entity.get_attr("executable", optional=True)
            or ""
        )
        ns_raw = entity.get_attr("namespace", optional=True)
        kwargs["namespace"] = parser.parse_substitution(ns_raw) if ns_raw else None
        kwargs["parameters"] = cls.parse_params(entity, parser)
        kwargs["remappings"] = cls.parse_remaps(entity, parser)
        kwargs["kind"] = "lifecycle_node" if entity.type_name == "lifecycle_node" else "node"
        kwargs["output"] = _parse_optional(parser, entity.get_attr("output", optional=True))
        kwargs["arguments"] = _parse_optional(parser, entity.get_attr("args", optional=True))
        kwargs["ros_arguments"] = _parse_optional(
            parser, entity.get_attr("ros_args", optional=True)
        )
        kwargs["respawn"] = _parse_optional(parser, entity.get_attr("respawn", optional=True))
        kwargs["respawn_delay"] = _parse_optional(
            parser, entity.get_attr("respawn_delay", optional=True)
        )
        return cls, kwargs

    def __init__(self, *, package=None, executable=None, name=None, **kwargs):
        self._kind = kwargs.pop("kind", None) or "node"
        # Build a dummy cmd matching the official Node(ExecuteProcess) pattern.
        # ExecutableInPackage cannot be supported in static analysis, so use a
        # ros2-run-style placeholder. Matching official: if package is given,
        # prefix with "ros2 run <package>"; otherwise start with executable.
        if package is not None:
            cmd: object = ["ros2", "run", package, executable]
        else:
            cmd = [executable]
        super().__init__(cmd=cmd, name=name, **kwargs)
        self.package = package
        self.executable = executable
        self.name = name
        self.namespace = kwargs.get("namespace")
        self.parameters: list = list(kwargs.get("parameters") or [])
        self.remappings: list = list(kwargs.get("remappings") or [])
        self.env: dict = {}
        self.param_files: list = []
        self.remap_metadata: dict[str, dict] = {}
        self.output = kwargs.get("output")
        self.args = kwargs.get("arguments")
        self.ros_args = kwargs.get("ros_arguments")
        self.respawn = kwargs.get("respawn")
        self.respawn_delay = kwargs.get("respawn_delay")
        self.ros_namespace: str | None = None
        self.explicit_namespace: str | None = None
        self.name_guessed: bool = False

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved Node."""
        state = context._state

        # Delegate cmd/name/env resolution to parent; cmd is ignored in Node.
        parent_result = super().execute(context)
        base = parent_result[0] if parent_result else None

        pkg = context.perform_substitution(self.package) or ""
        exe = context.perform_substitution(self.executable) or ""
        name = base.name if base else (context.perform_substitution(self.name) or "")
        ns = context.perform_substitution(self.namespace) if self.namespace else None
        name_guessed = not name and bool(exe)
        if name_guessed:
            logger.warning(
                "%s: node (pkg=%r exec=%r) has no name set; FQN is guessed from executable name",
                _current_file(context),
                pkg,
                exe,
            )
            name = exe
        if pkg:
            state.track_package(pkg)

        ros_ns = context._launch_configurations.get("ros_namespace")

        # Parameters: global first, then node-specific.
        # Deduplication: skip entries whose name+value are identical to the
        # current active_params value; keep entries that override a prior value so the
        # visualizer can show them as explicit overwrites.
        ctx_gp = context._launch_configurations.get("global_params", [])
        params: list[tuple[str, str]] = []
        active_params: dict[str, str] = {}

        def _push_param(k: str, v: str) -> None:
            if active_params.get(k) != v:
                params.append((k, v))
                active_params[k] = v

        for k, v in ctx_gp:
            _push_param(k, str(v))
        pf_list: list[dict] = list(context._launch_configurations.get("global_param_files", []))
        for p in self.parameters:
            if isinstance(p, ParameterFile):
                path_obj, expanded = p.evaluate(context)
                path = str(path_obj)
                state.track_param_file(path)
                pf_entry: dict = {"path": path, "params": expanded}
                pf_list.append(pf_entry)
                for k, v in expanded:
                    active_params[k] = str(v)
            elif isinstance(p, Parameter):
                k, v = p.evaluate(context)
                _push_param(k, v)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    _push_param(str(k), resolved_v if resolved_v is not None else "")

        remaps = list(context._launch_configurations.get("ros_remaps", []))
        for r in self.remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                remaps.append([src or str(r[0]), dst or str(r[1])])

        effective_ns = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        remap_metadata = state.apply_connection_plugin(
            pkg,
            dict(active_params),
            remaps,
            executable=exe,
            node_ns=effective_ns,
            node_name=name,
        )

        env = base.env if base else {}

        def _resolve_opt(attr):
            raw = getattr(self, attr, None)
            if raw is None:
                return None
            return context.perform_substitution(raw) or None

        resolved = type(self)(package=pkg, executable=exe, name=name or None)
        resolved.name_guessed = name_guessed
        resolved.ros_namespace = ros_ns
        resolved.explicit_namespace = ns
        resolved.namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved.parameters = params
        resolved.param_files = pf_list
        resolved.remappings = remaps
        resolved.remap_metadata = remap_metadata
        resolved.env = env
        resolved.output = _resolve_opt("output")
        resolved.args = _resolve_opt("args")
        resolved.ros_args = _resolve_opt("ros_args")
        resolved.respawn = _resolve_opt("respawn")
        resolved.respawn_delay = _resolve_opt("respawn_delay")
        return [resolved]

    _tag_name = "node"

    def serialize_resolved(self) -> list[ET.Element]:
        """Render this node as resolved XML elements."""
        if not self.package:
            return []

        elem = ET.Element(self._tag_name)
        elem.set("pkg", self.package)
        elem.set("exec", self.executable or "")
        if self.name and not self.name_guessed:
            elem.set("name", self.name)
        if self.namespace:
            elem.set("namespace", self.namespace)
        if self.output:
            elem.set("output", self.output)
        if self.args:
            elem.set("args", self.args)
        if self.ros_args:
            elem.set("ros_args", self.ros_args)
        if self.respawn:
            elem.set("respawn", self.respawn)
        if self.respawn_delay:
            elem.set("respawn_delay", self.respawn_delay)

        self._add_children(elem)
        return [elem]

    def _add_children(self, parent: ET.Element) -> None:
        """Add param_files, parameters, remappings, env as XML children."""
        from roscope.entities.helpers import _serialize_param_files, _serialize_remaps

        _serialize_param_files(parent, self.param_files)

        for key, value in self.parameters:
            p = ET.SubElement(parent, "param")
            p.set("name", key)
            p.set("value", value)

        ns = self.namespace
        qualified_remaps = [
            [
                from_,
                f"{ns.rstrip('/')}/{to}"
                if to and ns and not to.startswith("/") and not to.startswith("~/")
                else to,
            ]
            for from_, to in self.remappings
        ]
        _serialize_remaps(parent, qualified_remaps, self.remap_metadata)

        if isinstance(self.env, dict):
            for ename, value in sorted(self.env.items()):
                e = ET.SubElement(parent, "env")
                e.set("name", ename)
                e.set("value", value)

    def __repr__(self):
        return f"Node(package={self.package!r})"


@expose_action("lifecycle_node")
class LifecycleNode(Node):
    _tag_name = "lifecycle_node"

    def __init__(self, **kwargs):
        kwargs.setdefault("kind", "lifecycle_node")
        super().__init__(**kwargs)
        self._kind = "lifecycle_node"
