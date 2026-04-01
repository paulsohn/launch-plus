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


def _fully_qualified_name(node_entry: dict) -> str:
    """Compute fully-qualified node name from tracked entry.

    Matches official ``make_namespace_absolute(prefix_namespace(
    ros_namespace, prefix_namespace(node_namespace, name)))``.
    """

    ros_ns = node_entry.get("ros_namespace")
    explicit_ns = node_entry.get("explicit_namespace")
    name = node_entry.get("name", "")
    # Combine: ros_namespace + explicit_namespace + name
    ns = _ros2_namespace_join(ros_ns, explicit_ns) if explicit_ns else ros_ns
    return _ros2_namespace_join(ns, name) or ""


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
        self._idx = -1
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

    def _ensure_tracked(self, state) -> int:
        """Create the tracked node entry on first call, return index."""
        if self._idx >= 0:
            return int(self._idx)
        state.track_package(self._raw_package)
        for p in self._raw_parameters:
            if hasattr(p, "_param_file") and p._param_file:
                state.track_param_file(p._param_file)
        self._idx = state.track_node(
            {
                "package": str(self._raw_package) if self._raw_package else "",
                "executable": str(self._raw_executable) if self._raw_executable else "",
                "name": str(self._raw_name) if self._raw_name else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": self._kind,
                "plugins": [],
                "target": None,
            },
        )
        return int(self._idx)

    def execute(self, context) -> list | None:
        state = context._state
        self._ensure_tracked(state)
        if not self._resolved:
            self._resolved = True
            self._perform_substitutions(context)
            self._include_chain = list(state.include_chain)
            state.resolved_actions.append(self)
        return None

    def _perform_substitutions(self, context) -> None:
        """Resolve all substitutions into the tracked entry.

        Matching
        official ``Node._perform_substitutions(context)`` pattern.
        """
        state = context._state
        entry = state.tracked["nodes"][self._idx]

        # Package / executable / name
        pkg = context.perform_substitution(self._raw_package) or ""
        exe = context.perform_substitution(self._raw_executable) or ""
        name = context.perform_substitution(self._raw_name) or ""
        ns = context.perform_substitution(self._raw_namespace) if self._raw_namespace else None
        if pkg:
            state.track_package(pkg)
        entry["package"] = pkg
        entry["executable"] = exe
        entry["name"] = name
        entry["explicit_namespace"] = ns

        # Namespace from launch_configurations (matching official)
        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns

        # Store resolved data on instance for serialize_resolved()
        self._resolved_package = pkg
        self._resolved_executable = exe
        self._resolved_name = name or None
        self._resolved_namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns

        # Parameters: global first, then node-specific (matching official order)
        ctx_gp = context._launch_configurations.get("global_params", [])
        params: dict[str, str] = {k: str(v) for k, v in ctx_gp}
        global_pf = list(context._launch_configurations.get("global_param_files", []))
        pf_list: list[dict] = list(global_pf)

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

        entry["parameters"] = params
        entry["param_files"] = pf_list
        self._resolved_parameters = params
        self._resolved_param_files = pf_list

        # Remappings: global first, then node-specific (matching official)
        remaps = list(context._launch_configurations.get("ros_remaps", []))
        for r in self._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                remaps.append(
                    [
                        src if src is not None else str(r[0]),
                        dst if dst is not None else str(r[1]),
                    ]
                )
        entry["remappings"] = remaps
        self._resolved_remappings = remaps

        # Environment
        env = env_overrides(context)
        for item in self._raw_env or []:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                env[resolve_value(item[0], context) or ""] = resolve_value(item[1], context) or ""
        entry["env"] = env
        self._resolved_env = env

        # Extra fields
        self._resolved_output = None
        self._resolved_args = None
        self._resolved_respawn = None
        self._resolved_respawn_delay = None
        for attr, key in (
            ("_raw_output", "output"),
            ("_raw_arguments", "args"),
            ("_raw_respawn", "respawn"),
            ("_raw_respawn_delay", "respawn_delay"),
        ):
            raw = getattr(self, attr, None)
            if raw is not None:
                resolved = context.perform_substitution(raw)
                entry[key] = resolved if resolved else str(raw)
                setattr(self, f"_resolved_{key}", entry[key])

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
        self._idx = -1
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._composable_node_descriptions = list(composable_node_descriptions or [])
        self._resolved = False

    def _ensure_tracked(self, state) -> int:
        """Create the tracked container entry on first call, return index."""
        if self._idx >= 0:
            return int(self._idx)
        state.track_package(self._raw_package)
        for desc in self._composable_node_descriptions:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                state.track_package(raw_pkg)
        self._idx = state.track_node(
            {
                "package": str(self._raw_package) if self._raw_package else "",
                "executable": str(self._raw_executable) if self._raw_executable else "",
                "name": str(self._raw_name) if self._raw_name else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "container",
                "plugins": [],
                "target": None,
            },
        )
        return int(self._idx)

    def execute(self, context) -> list | None:
        state = context._state
        self._ensure_tracked(state)
        if not self._resolved:
            self._resolved = True
            self._perform_substitutions(context)
            self._include_chain = list(state.include_chain)
            state.resolved_actions.append(self)
        return None

    def _perform_substitutions(self, context) -> None:
        """Resolve all substitutions into the tracked entry."""

        state = context._state
        entry = state.tracked["nodes"][self._idx]

        pkg = context.perform_substitution(self._raw_package) or ""
        exe = context.perform_substitution(self._raw_executable) or ""
        name = context.perform_substitution(self._raw_name) or ""
        ns = context.perform_substitution(self._raw_namespace) if self._raw_namespace else None
        if pkg:
            state.track_package(pkg)
        entry["package"] = pkg
        entry["executable"] = exe
        entry["name"] = name
        entry["explicit_namespace"] = ns
        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns

        # Store resolved data on instance
        self._resolved_package = pkg
        self._resolved_executable = exe
        self._resolved_name = name or None
        self._resolved_namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns

        # Params (global only for container)
        ctx_gp = context._launch_configurations.get("global_params", [])
        params = {k: str(v) for k, v in ctx_gp}
        pf_list = list(context._launch_configurations.get("global_param_files", []))
        remaps = list(context._launch_configurations.get("ros_remaps", []))
        entry["parameters"] = params
        entry["param_files"] = pf_list
        entry["remappings"] = remaps
        self._resolved_parameters = params
        self._resolved_param_files = pf_list
        self._resolved_remappings = remaps

        # Env
        env = env_overrides(context)
        for item in self._raw_env or []:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                env[resolve_value(item[0], context) or ""] = resolve_value(item[1], context) or ""
        entry["env"] = env
        self._resolved_env = env

        entry["plugins"] = _resolve_plugins(self._composable_node_descriptions, context)

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
        self._idx = -1
        self._raw_target = target_container
        self._raw_namespace = kwargs.get("namespace")
        self._composable_node_descriptions = list(composable_node_descriptions or [])
        self._resolved = False

    def _ensure_tracked(self, state) -> int:
        """Create the tracked load_composable entry on first call, return index."""
        if self._idx >= 0:
            return int(self._idx)
        for desc in self._composable_node_descriptions:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                state.track_package(raw_pkg)
        self._idx = state.track_node(
            {
                "package": "",
                "executable": "",
                "name": "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "load_composable",
                "plugins": [],
                "target": "",
            },
        )
        return int(self._idx)

    def execute(self, context) -> list | None:
        state = context._state
        self._ensure_tracked(state)
        if not self._resolved:
            self._resolved = True
            self._perform_substitutions(context)
            self._include_chain = list(state.include_chain)
            state.resolved_actions.append(self)
        return None

    def _perform_substitutions(self, context) -> None:
        """Resolve all substitutions into the tracked entry."""
        state = context._state
        entry = state.tracked["nodes"][self._idx]

        # Resolve target
        target = ""
        if self._raw_target is not None:
            if isinstance(self._raw_target, ComposableNodeContainer):
                self._raw_target._ensure_tracked(state)
                container = state.tracked["nodes"][self._raw_target._idx]
                target = _fully_qualified_name(container)
            else:
                target = context.perform_substitution(self._raw_target) or ""
            entry["target"] = target

        ns = context.perform_substitution(self._raw_namespace) if self._raw_namespace else None
        entry["explicit_namespace"] = ns
        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns

        entry["plugins"] = _resolve_plugins(self._composable_node_descriptions, context)

        # Store resolved data on instance
        self._resolved_target = target
        self._resolved_namespace = _ros2_namespace_join(ros_ns, ns) if ns else ros_ns

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
