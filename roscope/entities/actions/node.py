"""Action handlers for node-related elements.

Covers: <node>, <lifecycle_node>, <node_container>,
<composable_node_container>, <load_composable_node>.
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


def _resolve_plugin(desc_or_dict, context) -> dict:
    """Resolve a single ComposableNode to output dict."""

    state = context._state
    params: dict[str, str] = {}
    pf_list: list[dict] = []
    remaps: list = []

    if isinstance(desc_or_dict, ComposableNode):
        desc = desc_or_dict
        pkg = context.perform_substitution(desc.package) or str(desc.package or "")
        plugin_name = context.perform_substitution(desc.plugin) or str(desc.plugin or "")
        name = context.perform_substitution(desc.name) or None
        # Compute effective namespace for remap qualification
        ros_ns = context._launch_configurations.get("ros_namespace")
        node_ns = None
        if getattr(desc, "namespace", None):
            node_ns = context.perform_substitution(desc.namespace)
        full_ns = _ros2_namespace_join(ros_ns, node_ns) if node_ns else ros_ns
        for p in desc.parameters:
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
        # Remaps — qualify 'to' with namespace
        for r in desc.remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                src = src if src is not None else str(r[0])
                dst = dst if dst is not None else str(r[1])
                if dst and full_ns and not dst.startswith("/") and not dst.startswith("~/"):
                    dst = f"{full_ns.rstrip('/')}/{dst}"
                remaps.append([src, dst])
    else:
        # Unknown description object — best effort
        pkg = str(getattr(desc_or_dict, "package", "") or "")
        plugin_name = str(getattr(desc_or_dict, "plugin", "") or "")
        name = None
        full_ns = None

    if pkg:
        state.track_package(pkg)

    entry: dict = {
        "package": pkg,
        "plugin": plugin_name,
        "name": name,
        "namespace": full_ns,
        "parameters": params,
        "remappings": remaps,
    }
    if pf_list:
        entry["param_files"] = pf_list
    # Store resolved data back on the ComposableNode for serialize_resolved()
    if isinstance(desc_or_dict, ComposableNode):
        desc_or_dict._resolved_data = entry
    return entry


def _resolve_plugins(items, context) -> list[dict]:
    """Resolve a list of ComposableNode descriptions to output dicts."""
    return [_resolve_plugin(item, context) for item in (items or [])]


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
        items = entity.get_attr("composable_node", data_type=list, optional=True)
        if not items:
            return []
        plugins = []
        for cn in items:
            if not parser.evaluate_condition(cn):
                continue
            name_raw = cn.get_attr("name", optional=True)
            ns_raw = cn.get_attr("namespace", optional=True)
            plugins.append(
                ComposableNode(
                    package=parser.parse_substitution(cn.get_attr("pkg", optional=True) or ""),
                    plugin=parser.parse_substitution(cn.get_attr("plugin", optional=True) or ""),
                    name=parser.parse_substitution(name_raw) if name_raw else None,
                    namespace=parser.parse_substitution(ns_raw) if ns_raw else None,
                    parameters=Node.parse_params(cn, parser),
                    remappings=Node.parse_remaps(cn, parser),
                )
            )
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


class ComposableNode(Action):
    """A composable node plugin loaded into a container process.

    Does NOT add to the flat ``_state.tracked["nodes"]`` list — it is attached to the
    container's ``plugins`` list when the container is resolved in ``execute()``.
    """

    def __init__(self, *, package=None, plugin=None, name=None, namespace=None, **kwargs):
        self.package = package
        self.plugin = plugin
        self.name = name
        self.namespace = namespace
        self.parameters: list | dict = list(kwargs.get("parameters") or [])
        self.remappings: list = list(kwargs.get("remappings") or [])
        self.param_files: list = []
        self._resolved_data: dict | None = None

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

        for pf in data.get("param_files", []):
            path = pf.get("path", "")
            inlined = pf.get("params")
            if inlined is not None:
                elem.append(ET.Comment(f" params from: {path} "))
                for k, v in inlined:
                    p = ET.SubElement(elem, "param")
                    p.set("name", k)
                    p.set("value", str(v))
                elem.append(ET.Comment(f" end params from: {path} "))
            else:
                p = ET.SubElement(elem, "param")
                p.set("from", path)
        for k, v in sorted(data.get("parameters", {}).items()):
            p = ET.SubElement(elem, "param")
            p.set("name", k)
            p.set("value", v)
        for from_, to in data.get("remappings", []):
            r = ET.SubElement(elem, "remap")
            r.set("from", from_)
            r.set("to", to)

        return [elem]

    def __repr__(self):
        return f"ComposableNode(package={self.package!r}, plugin={self.plugin!r})"


@expose_action("node_container")
@expose_action("composable_node_container")
class ComposableNodeContainer(Action):
    """A composable node container process.

    Emits a ``kind='container'`` entry whose ``plugins`` list is populated during
    deferred resolution in ``execute()`` from the *composable_node_descriptions*.
    """

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
        kwargs["env"] = Node.parse_envs(entity, parser)
        kwargs["composable_node_descriptions"] = Node.parse_composable_plugins(entity, parser)
        # Consumed by Node/ExecuteProcess.parse() in the official inheritance chain
        # (ComposableNodeContainer → Node → ExecuteProcess). Roscope inherits from
        # Action directly, so parse them explicitly and preserve in output.
        kwargs["output"] = _parse_optional(parser, entity.get_attr("output", optional=True))
        kwargs["arguments"] = _parse_optional(parser, entity.get_attr("args", optional=True))
        kwargs["ros_arguments"] = _parse_optional(
            parser, entity.get_attr("ros_args", optional=True)
        )
        return cls, kwargs

    def __init__(
        self,
        *,
        package=None,
        executable=None,
        name=None,
        composable_node_descriptions=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.package = package
        self.executable = executable
        self.name = name
        self.namespace = kwargs.get("namespace")
        self.env: list | dict = kwargs.get("env") or []
        self.composable_node_descriptions: list = list(composable_node_descriptions or [])
        self.parameters: dict = {}
        self.param_files: list = []
        self.remappings: list = []
        self.output = kwargs.get("output")
        self.args = kwargs.get("arguments")
        self.ros_args = kwargs.get("ros_arguments")
        self.ros_namespace: str | None = None
        self.explicit_namespace: str | None = None

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved Container."""
        state = context._state

        pkg = context.perform_substitution(self.package) or ""
        exe = context.perform_substitution(self.executable) or ""
        name = context.perform_substitution(self.name) or ""
        ns = context.perform_substitution(self.namespace) if self.namespace else None
        if pkg:
            state.track_package(pkg)
        for desc in self.composable_node_descriptions:
            raw_pkg = getattr(desc, "package", None)
            if raw_pkg:
                state.track_package(raw_pkg)

        ros_ns = context._launch_configurations.get("ros_namespace")
        ctx_gp = context._launch_configurations.get("global_params", [])

        env = env_overrides(context)
        for item in self.env if isinstance(self.env, list) else []:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                env[resolve_value(item[0], context) or ""] = resolve_value(item[1], context) or ""

        _resolve_plugins(self.composable_node_descriptions, context)

        def _resolve_opt(attr):
            raw = getattr(self, attr, None)
            if raw is None:
                return None
            return context.perform_substitution(raw) or None

        resolved = ComposableNodeContainer(package=pkg, executable=exe, name=name or None)
        resolved.ros_namespace = ros_ns
        resolved.explicit_namespace = ns
        resolved.namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved.parameters = {k: str(v) for k, v in ctx_gp}
        resolved.param_files = list(context._launch_configurations.get("global_param_files", []))
        resolved.remappings = list(context._launch_configurations.get("ros_remaps", []))
        resolved.env = env
        resolved.output = _resolve_opt("output")
        resolved.args = _resolve_opt("args")
        resolved.ros_args = _resolve_opt("ros_args")
        resolved.composable_node_descriptions = self.composable_node_descriptions

        # Store FQN on the original object for LoadComposableNodes.
        # Matching official: the container internally stores its fully qualified
        # node name so that load actions in different scopes can reference it.
        self.fqn = _ros2_namespace_join(resolved.namespace, resolved.name) or ""

        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.package:
            return []

        elem = ET.Element("node_container")
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

        for pf in self.param_files:
            path = pf.get("path", "")
            inlined = pf.get("params")
            if inlined is not None:
                elem.append(ET.Comment(f" params from: {path} "))
                for k, v in inlined:
                    p = ET.SubElement(elem, "param")
                    p.set("name", k)
                    p.set("value", str(v))
                elem.append(ET.Comment(f" end params from: {path} "))
        for k, v in sorted(self.parameters.items()):
            p = ET.SubElement(elem, "param")
            p.set("name", k)
            p.set("value", v)
        for from_, to in self.remappings:
            r = ET.SubElement(elem, "remap")
            r.set("from", from_)
            r.set("to", to)
        for name, value in sorted(self.env.items()) if isinstance(self.env, dict) else []:
            e = ET.SubElement(elem, "env")
            e.set("name", name)
            e.set("value", value)

        # Composable plugins
        for desc in self.composable_node_descriptions:
            for child_elem in desc.serialize_resolved():
                elem.append(child_elem)

        return [elem]


@expose_action("load_composable_node")
class LoadComposableNodes(Action):
    """Loads composable nodes into an existing container."""

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

        _resolve_plugins(self.composable_node_descriptions, context)

        resolved = LoadComposableNodes()
        resolved.target = target
        resolved.ros_namespace = ros_ns
        resolved.explicit_namespace = ns
        resolved.namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved.composable_node_descriptions = self.composable_node_descriptions
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
