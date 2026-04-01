"""Action handlers for node-related elements.

Covers: <node>, <lifecycle_node>, <node_container>,
<composable_node_container>, <load_composable_node>.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.helpers import (
    _read_and_expand_param_file,
    _ros2_namespace_join,
    env_overrides,
    resolve_value,
)
from launch_plus.entities.parameter_descriptions import Parameter, ParameterFile
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity


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
        pkg = context.perform_substitution(desc._raw_package) or desc._package
        plugin_name = context.perform_substitution(desc._raw_plugin) or desc._plugin
        name = context.perform_substitution(desc._raw_name) or desc._name or None
        for p in desc._raw_parameters:
            if isinstance(p, ParameterFile):
                path = p.evaluate(context)
                state.track_param_file(path)
                pf_entry: dict = {"path": path}
                if state.inline_params:
                    expanded = _read_and_expand_param_file(path, context)
                    if expanded is not None:
                        pf_entry["params"] = expanded
                pf_list.append(pf_entry)
            elif isinstance(p, Parameter):
                k, v = p.evaluate(context)
                params[k] = v
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    params[str(k)] = resolved_v if resolved_v is not None else ""
        # Remaps
        for r in desc._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                remaps.append(
                    [
                        src if src is not None else str(r[0]),
                        dst if dst is not None else str(r[1]),
                    ]
                )
    else:
        # Unknown description object — best effort
        pkg = str(
            getattr(desc_or_dict, "package", None) or getattr(desc_or_dict, "_package", "") or ""
        )
        plugin_name = str(
            getattr(desc_or_dict, "plugin", None) or getattr(desc_or_dict, "_plugin", "") or ""
        )
        name = None

    if pkg:
        state.track_package(pkg)

    entry: dict = {
        "package": pkg,
        "plugin": plugin_name,
        "name": name,
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

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        pkg = parser.parse_substitution(
            entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
        )
        exe = parser.parse_substitution(
            entity.get_attr("exec", optional=True)
            or entity.get_attr("executable", optional=True)
            or ""
        )
        name_raw = entity.get_attr("name", optional=True)
        ns_raw = entity.get_attr("namespace", optional=True)
        node_kind = "lifecycle_node" if entity.type_name == "lifecycle_node" else "node"
        return cls(
            package=pkg,
            executable=exe,
            name=parser.parse_substitution(name_raw) if name_raw else None,
            namespace=parser.parse_substitution(ns_raw) if ns_raw else None,
            parameters=parser.parse_params(entity),
            remappings=parser.parse_remaps(entity),
            env=parser.parse_envs(entity),
            kind=node_kind,
            output=_parse_optional(parser, entity.get_attr("output", optional=True)),
            arguments=_parse_optional(parser, entity.get_attr("args", optional=True)),
            respawn=_parse_optional(parser, entity.get_attr("respawn", optional=True)),
            respawn_delay=_parse_optional(parser, entity.get_attr("respawn_delay", optional=True)),
        )

    def __init__(self, *, package=None, executable=None, name=None, **kwargs):
        self._kind = kwargs.pop("kind", None) or "node"
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._raw_output = kwargs.get("output")
        self._raw_arguments = kwargs.get("arguments")
        self._raw_respawn = kwargs.get("respawn")
        self._raw_respawn_delay = kwargs.get("respawn_delay")
        self._resolved = False
        self._resolved_package: str = ""
        self._resolved_executable: str = ""
        self._resolved_name: str | None = None
        self._resolved_ros_namespace: str | None = None
        self._resolved_explicit_namespace: str | None = None
        self._resolved_namespace: str | None = None
        self._resolved_parameters: dict = {}
        self._resolved_param_files: list = []
        self._resolved_remappings: list = []
        self._resolved_env: dict = {}
        self._resolved_output: str | None = None
        self._resolved_args: str | None = None
        self._resolved_respawn: str | None = None
        self._resolved_respawn_delay: str | None = None

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved Node."""
        state = context._state

        pkg = context.perform_substitution(self._raw_package) or ""
        exe = context.perform_substitution(self._raw_executable) or ""
        name = context.perform_substitution(self._raw_name) or ""
        ns = context.perform_substitution(self._raw_namespace) if self._raw_namespace else None
        if pkg:
            state.track_package(pkg)

        ros_ns = context._launch_configurations.get("ros_namespace")

        # Parameters: global first, then node-specific
        ctx_gp = context._launch_configurations.get("global_params", [])
        params: dict[str, str] = {k: str(v) for k, v in ctx_gp}
        pf_list: list[dict] = list(context._launch_configurations.get("global_param_files", []))
        for p in self._raw_parameters:
            if isinstance(p, ParameterFile):
                path = p.evaluate(context)
                state.track_param_file(path)
                pf_entry: dict = {"path": path}
                if state.inline_params:
                    expanded = _read_and_expand_param_file(path, context)
                    if expanded is not None:
                        pf_entry["params"] = expanded
                pf_list.append(pf_entry)
            elif isinstance(p, Parameter):
                k, v = p.evaluate(context)
                params[k] = v
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    params[str(k)] = resolved_v if resolved_v is not None else ""

        remaps = list(context._launch_configurations.get("ros_remaps", []))
        for r in self._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                remaps.append([src or str(r[0]), dst or str(r[1])])

        env = env_overrides(context)
        for item in self._raw_env or []:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                env[resolve_value(item[0], context) or ""] = resolve_value(item[1], context) or ""

        def _resolve_opt(attr):
            raw = getattr(self, attr, None)
            if raw is None:
                return None
            r = context.perform_substitution(raw)
            return r if r else str(raw)

        resolved = type(self)(package=pkg, executable=exe, name=name or "")
        resolved._resolved = True
        resolved._resolved_package = pkg
        resolved._resolved_executable = exe
        resolved._resolved_name = name or None
        resolved._resolved_ros_namespace = ros_ns
        resolved._resolved_explicit_namespace = ns
        resolved._resolved_namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved._resolved_parameters = params
        resolved._resolved_param_files = pf_list
        resolved._resolved_remappings = remaps
        resolved._resolved_env = env
        resolved._resolved_output = _resolve_opt("_raw_output")
        resolved._resolved_args = _resolve_opt("_raw_arguments")
        resolved._resolved_respawn = _resolve_opt("_raw_respawn")
        resolved._resolved_respawn_delay = _resolve_opt("_raw_respawn_delay")
        return [resolved]

    _tag_name = "node"

    def serialize_resolved(self) -> list[ET.Element]:
        """Render this node as resolved XML elements."""
        if not self._resolved:
            return []

        elem = ET.Element(self._tag_name)
        elem.set("pkg", self._resolved_package)
        elem.set("exec", self._resolved_executable)
        if self._resolved_name:
            elem.set("name", self._resolved_name)
        if self._resolved_namespace:
            elem.set("namespace", self._resolved_namespace)
        if self._resolved_output:
            elem.set("output", self._resolved_output)
        if self._resolved_args:
            elem.set("args", self._resolved_args)
        if self._resolved_respawn:
            elem.set("respawn", self._resolved_respawn)
        if self._resolved_respawn_delay:
            elem.set("respawn_delay", self._resolved_respawn_delay)

        self._add_children(elem)
        return [elem]

    def _add_children(self, parent: ET.Element) -> None:
        """Add param_files, parameters, remappings, env as XML children."""
        for pf in getattr(self, "_resolved_param_files", []):
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

        for key, value in sorted(getattr(self, "_resolved_parameters", {}).items()):
            p = ET.SubElement(parent, "param")
            p.set("name", key)
            p.set("value", value)

        ns = getattr(self, "_resolved_namespace", None)
        for from_, to in getattr(self, "_resolved_remappings", []):
            # Only qualify 'to' — 'from' is a node-internal name
            if to and ns and not to.startswith("/") and not to.startswith("~/"):
                to = f"{ns.rstrip('/')}/{to}"
            r = ET.SubElement(parent, "remap")
            r.set("from", from_)
            r.set("to", to)

        for name, value in sorted(getattr(self, "_resolved_env", {}).items()):
            e = ET.SubElement(parent, "env")
            e.set("name", name)
            e.set("value", value)

    def __repr__(self):
        return f"Node(package={self._raw_package!r})"


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

    _resolved_data: dict | None = None

    def __init__(self, *, package=None, plugin=None, name=None, **kwargs):
        self._raw_package = package
        self._package = str(package) if package else ""
        self._raw_plugin = plugin
        self._plugin = str(plugin) if plugin else ""
        self._raw_name = name
        self._name = str(name) if name else ""
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])

    def serialize_resolved(self) -> list[ET.Element]:
        """Render as <composable_node> element (called by parent container)."""
        data = getattr(self, "_resolved_data", None)
        if data is None:
            return []

        elem = ET.Element("composable_node")
        elem.set("pkg", data.get("package", ""))
        elem.set("plugin", data.get("plugin", ""))
        name = data.get("name")
        if name:
            elem.set("name", name)

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
        return f"ComposableNode(package={self._package!r}, plugin={self._plugin!r})"


@expose_action("node_container")
@expose_action("composable_node_container")
class ComposableNodeContainer(Action):
    """A composable node container process.

    Emits a ``kind='container'`` entry whose ``plugins`` list is populated during
    deferred resolution in ``execute()`` from the *composable_node_descriptions*.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        pkg = parser.parse_substitution(
            entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
        )
        exe = parser.parse_substitution(
            entity.get_attr("exec", optional=True)
            or entity.get_attr("executable", optional=True)
            or ""
        )
        name_raw = entity.get_attr("name", optional=True)
        ns_raw = entity.get_attr("namespace", optional=True)
        return cls(
            package=pkg,
            executable=exe,
            name=parser.parse_substitution(name_raw) if name_raw else None,
            namespace=parser.parse_substitution(ns_raw) if ns_raw else None,
            env=parser.parse_envs(entity),
            composable_node_descriptions=parser.parse_composable_plugins(entity),
        )

    def __init__(
        self,
        *,
        package=None,
        executable=None,
        name=None,
        composable_node_descriptions=None,
        **kwargs,
    ):
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._composable_node_descriptions = list(composable_node_descriptions or [])
        self._resolved = False
        self._resolved_package: str = ""
        self._resolved_executable: str = ""
        self._resolved_name: str | None = None
        self._resolved_ros_namespace: str | None = None
        self._resolved_explicit_namespace: str | None = None
        self._resolved_namespace: str | None = None
        self._resolved_parameters: dict = {}
        self._resolved_param_files: list = []
        self._resolved_remappings: list = []
        self._resolved_env: dict = {}
        self.fqn: str = ""

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved Container."""
        state = context._state

        pkg = context.perform_substitution(self._raw_package) or ""
        exe = context.perform_substitution(self._raw_executable) or ""
        name = context.perform_substitution(self._raw_name) or ""
        ns = context.perform_substitution(self._raw_namespace) if self._raw_namespace else None
        if pkg:
            state.track_package(pkg)
        for desc in self._composable_node_descriptions:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                state.track_package(raw_pkg)

        ros_ns = context._launch_configurations.get("ros_namespace")
        ctx_gp = context._launch_configurations.get("global_params", [])

        env = env_overrides(context)
        for item in self._raw_env or []:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                env[resolve_value(item[0], context) or ""] = resolve_value(item[1], context) or ""

        _resolve_plugins(self._composable_node_descriptions, context)

        resolved = ComposableNodeContainer(package=pkg, executable=exe, name=name or "")
        resolved._resolved = True
        resolved._resolved_package = pkg
        resolved._resolved_executable = exe
        resolved._resolved_name = name or None
        resolved._resolved_ros_namespace = ros_ns
        resolved._resolved_explicit_namespace = ns
        resolved._resolved_namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved._resolved_parameters = {k: str(v) for k, v in ctx_gp}
        resolved._resolved_param_files = list(
            context._launch_configurations.get("global_param_files", [])
        )
        resolved._resolved_remappings = list(context._launch_configurations.get("ros_remaps", []))
        resolved._resolved_env = env
        resolved._composable_node_descriptions = self._composable_node_descriptions

        # Publish FQN on the original object for cross-references
        # (LoadComposableNodes reads this to identify the target container)
        self.fqn = _ros2_namespace_join(resolved._resolved_namespace, resolved._resolved_name) or ""

        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self._resolved:
            return []

        elem = ET.Element("node_container")
        elem.set("pkg", self._resolved_package)
        elem.set("exec", self._resolved_executable)
        if self._resolved_name:
            elem.set("name", self._resolved_name)
        if self._resolved_namespace:
            elem.set("namespace", self._resolved_namespace)

        # Node children (params, remaps, env)
        for pf in self._resolved_param_files:
            path = pf.get("path", "")
            inlined = pf.get("params")
            if inlined is not None:
                elem.append(ET.Comment(f" params from: {path} "))
                for k, v in inlined:
                    p = ET.SubElement(elem, "param")
                    p.set("name", k)
                    p.set("value", str(v))
                elem.append(ET.Comment(f" end params from: {path} "))
        for k, v in sorted(self._resolved_parameters.items()):
            p = ET.SubElement(elem, "param")
            p.set("name", k)
            p.set("value", v)
        for from_, to in self._resolved_remappings:
            r = ET.SubElement(elem, "remap")
            r.set("from", from_)
            r.set("to", to)
        for name, value in sorted(self._resolved_env.items()):
            e = ET.SubElement(elem, "env")
            e.set("name", name)
            e.set("value", value)

        # Composable plugins
        for desc in self._composable_node_descriptions:
            for child_elem in desc.serialize_resolved():
                elem.append(child_elem)

        return [elem]


@expose_action("load_composable_node")
class LoadComposableNodes(Action):
    """Loads composable nodes into an existing container."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        target_raw = entity.get_attr("target", optional=True)
        ns_raw = entity.get_attr("namespace", optional=True)
        return cls(
            target_container=_parse_optional(parser, target_raw),
            composable_node_descriptions=parser.parse_composable_plugins(entity),
            namespace=_parse_optional(parser, ns_raw),
        )

    def __init__(self, *, composable_node_descriptions=None, target_container=None, **kwargs):
        self._raw_target = target_container
        self._raw_namespace = kwargs.get("namespace")
        self._composable_node_descriptions = list(composable_node_descriptions or [])
        self._resolved = False
        self._resolved_target: str = ""
        self._resolved_ros_namespace: str | None = None
        self._resolved_explicit_namespace: str | None = None
        self._resolved_namespace: str | None = None

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved LoadComposableNodes."""
        state = context._state

        for desc in self._composable_node_descriptions:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                state.track_package(raw_pkg)

        target = ""
        if self._raw_target is not None:
            if isinstance(self._raw_target, ComposableNodeContainer):
                target = getattr(self._raw_target, "fqn", "")
            else:
                target = context.perform_substitution(self._raw_target) or ""

        ns = context.perform_substitution(self._raw_namespace) if self._raw_namespace else None
        ros_ns = context._launch_configurations.get("ros_namespace")

        _resolve_plugins(self._composable_node_descriptions, context)

        resolved = LoadComposableNodes()
        resolved._resolved = True
        resolved._resolved_target = target
        resolved._resolved_ros_namespace = ros_ns
        resolved._resolved_explicit_namespace = ns
        resolved._resolved_namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns
        resolved._composable_node_descriptions = self._composable_node_descriptions
        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self._resolved:
            return []
        if not self._composable_node_descriptions:
            return []

        elem = ET.Element("load_composable_node")
        target = self._resolved_target or ""
        if target:
            elem.set("target", target)

        for desc in self._composable_node_descriptions:
            for child_elem in desc.serialize_resolved():
                elem.append(child_elem)

        return [elem]
