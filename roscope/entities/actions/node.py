"""Action handler for <node> and <lifecycle_node>.

Matching official ``launch_ros.actions.Node`` and ``LifecycleNode``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.helpers import (
    _ros2_namespace_join,
    env_overrides,
    resolve_value,
)
from roscope.entities.parameter_descriptions import Parameter, ParameterFile
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity


def _parse_optional(parser: _ActionParser, text: str | None) -> list | None:
    """Parse an optional attribute to tokens, or return None."""
    if text is None:
        return None
    return parser.parse_substitution(text)


@expose_action("node")
@expose_action("lifecycle_node")
class Node(Action):
    """Tracks a Node / LifecycleNode ."""

    @staticmethod
    def parse_params(entity: Entity, parser: _ActionParser) -> list:
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
    def parse_remaps(entity: Entity, parser: _ActionParser) -> list:
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
    def parse_envs(entity: Entity, parser: _ActionParser) -> list:
        """Extract <env> children as unresolved token pairs."""
        items = entity.get_attr("env", data_type=list, optional=True)
        if not items:
            return []
        return [
            (
                parser.parse_substitution(e.get_attr("name", optional=True) or ""),
                parser.parse_substitution(e.get_attr("value", optional=True) or ""),
            )
            for e in items
        ]

    @staticmethod
    def parse_composable_plugins(entity: Entity, parser: _ActionParser) -> list:
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
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["package"] = parser.parse_substitution(
            entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
        )
        kwargs["executable"] = parser.parse_substitution(
            entity.get_attr("exec", optional=True)
            or entity.get_attr("executable", optional=True)
            or ""
        )
        name_raw = entity.get_attr("name", optional=True)
        ns_raw = entity.get_attr("namespace", optional=True)
        kwargs["name"] = parser.parse_substitution(name_raw) if name_raw else None
        kwargs["namespace"] = parser.parse_substitution(ns_raw) if ns_raw else None
        kwargs["parameters"] = cls.parse_params(entity, parser)
        kwargs["remappings"] = cls.parse_remaps(entity, parser)
        kwargs["env"] = cls.parse_envs(entity, parser)
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
        super().__init__(**kwargs)
        self._kind = kwargs.pop("kind", None) or "node"
        self.package = package
        self.executable = executable
        self.name = name
        self.namespace = kwargs.get("namespace")
        self.parameters: list | dict = list(kwargs.get("parameters") or [])
        self.remappings: list = list(kwargs.get("remappings") or [])
        self.env: list | dict = kwargs.get("env") or []
        self.param_files: list = []
        self.output = kwargs.get("output")
        self.args = kwargs.get("arguments")
        self.ros_args = kwargs.get("ros_arguments")
        self.respawn = kwargs.get("respawn")
        self.respawn_delay = kwargs.get("respawn_delay")
        self.ros_namespace: str | None = None
        self.explicit_namespace: str | None = None

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved Node."""
        state = context._state

        pkg = context.perform_substitution(self.package) or ""
        exe = context.perform_substitution(self.executable) or ""
        name = context.perform_substitution(self.name) or ""
        ns = context.perform_substitution(self.namespace) if self.namespace else None
        if pkg:
            state.track_package(pkg)

        ros_ns = context._launch_configurations.get("ros_namespace")

        # Parameters: global first, then node-specific
        ctx_gp = context._launch_configurations.get("global_params", [])
        params: dict[str, str] = {k: str(v) for k, v in ctx_gp}
        pf_list: list[dict] = list(context._launch_configurations.get("global_param_files", []))
        for p in self.parameters:
            if isinstance(p, ParameterFile):
                path_obj, expanded = p.evaluate(context)
                path = str(path_obj)
                state.track_param_file(path)
                pf_entry: dict = {"path": path, "params": expanded}
                pf_list.append(pf_entry)
            elif isinstance(p, Parameter):
                k, v = p.evaluate(context)
                params[k] = v
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    params[str(k)] = resolved_v if resolved_v is not None else ""

        remaps = list(context._launch_configurations.get("ros_remaps", []))
        for r in self.remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                remaps.append([src or str(r[0]), dst or str(r[1])])

        env = env_overrides(context)
        for item in self.env:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                env[resolve_value(item[0], context) or ""] = resolve_value(item[1], context) or ""

        def _resolve_opt(attr):
            raw = getattr(self, attr, None)
            if raw is None:
                return None
            return context.perform_substitution(raw) or None

        resolved = type(self)(package=pkg, executable=exe, name=name or None)
        resolved.ros_namespace = ros_ns
        resolved.explicit_namespace = ns
        resolved.namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved.parameters = params
        resolved.param_files = pf_list
        resolved.remappings = remaps
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
        if self.name:
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
        for pf in self.param_files:
            path = pf.get("path", "")
            inlined = pf.get("params")
            if inlined is not None:
                parent.append(ET.Comment(f" params from: {path} "))
                for k, v in inlined:
                    p = ET.SubElement(parent, "param")
                    p.set("name", k)
                    p.set("value", str(v))
                parent.append(ET.Comment(f" end params from: {path} "))
            else:
                p = ET.SubElement(parent, "param")
                p.set("from", path)

        if isinstance(self.parameters, dict):
            for key, value in sorted(self.parameters.items()):
                p = ET.SubElement(parent, "param")
                p.set("name", key)
                p.set("value", value)

        for from_, to in self.remappings:
            if to and self.namespace and not to.startswith("/") and not to.startswith("~/"):
                to = f"{self.namespace.rstrip('/')}/{to}"
            r = ET.SubElement(parent, "remap")
            r.set("from", from_)
            r.set("to", to)

        if isinstance(self.env, dict):
            for ename, value in sorted(self.env.items()):
                e = ET.SubElement(parent, "env")
                e.set("name", ename)
                e.set("value", value)

    def __repr__(self):
        return f"Node(package={self.package!r})"


class LifecycleNode(Node):
    _tag_name = "lifecycle_node"

    def __init__(self, **kwargs):
        kwargs.setdefault("kind", "lifecycle_node")
        super().__init__(**kwargs)
        self._kind = "lifecycle_node"
