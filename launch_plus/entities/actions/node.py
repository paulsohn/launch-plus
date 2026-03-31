"""Action handlers for node-related elements.

Covers: <node>, <lifecycle_node>, <node_container>,
<composable_node_container>, <load_composable_node>.
"""

from __future__ import annotations

from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity


def _parse_optional(parser: _ActionParser, text: str | None) -> list | None:
    """Parse an optional attribute to tokens, or return None."""
    if text is None:
        return None
    return parser.parse_substitution(text)


def _resolve_xml_composable_plugins(
    xml_plugins: list[dict],
    context,
) -> list[dict]:
    """Resolve XML-parsed composable plugin token dicts into final form."""
    from launch_plus.entities.helpers import (
        _read_and_expand_param_file,
        resolve_value,
    )

    plugins: list[dict] = []
    for p in xml_plugins or []:
        pkg = resolve_value(p["package"], context) or ""
        plugin_name = resolve_value(p["plugin"], context) or ""
        name_raw = p.get("name")
        name = resolve_value(_parse_optional_raw(name_raw), context) if name_raw else None
        state = context._state
        if pkg:
            state.track_package(pkg)
        # Resolve params
        params: dict[str, str] = {}
        param_files: list[dict] = []
        for param in p.get("params") or []:
            if "from" in param:
                path = resolve_value(param["from"], context) or ""
                state.track_param_file(path)
                pf_entry: dict = {"path": path}
                if state.inline_params:
                    expanded = _read_and_expand_param_file(path, context)
                    if expanded is not None:
                        pf_entry["params"] = expanded
                param_files.append(pf_entry)
            else:
                k = resolve_value(param["name"], context) or ""
                v = resolve_value(param["value"], context) or ""
                params[k] = v
        # Resolve remaps
        remaps = [
            [resolve_value(src, context) or "", resolve_value(dst, context) or ""]
            for src, dst in (p.get("remaps") or [])
        ]
        entry: dict = {
            "package": pkg,
            "plugin": plugin_name,
            "name": name,
            "parameters": params,
            "remappings": remaps,
        }
        if param_files:
            entry["param_files"] = param_files
        plugins.append(entry)
    return plugins


def _parse_optional_raw(text: str | None) -> list | None:
    """Parse an optional raw text to tokens using the Lark parser directly."""
    if text is None:
        return None
    from launch_plus.parsers.parse_substitution import parse_substitution as _lark_parse

    return _lark_parse(text)


@expose_action("node")
@expose_action("lifecycle_node")
class _TrackedNode(_TrackedAction):
    """Tracks a Node / LifecycleNode for both XML and Python shim paths."""

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
            _xml_params=parser.parse_params(entity),
            _xml_remaps=parser.parse_remaps(entity),
            _xml_envs=parser.parse_envs(entity),
            _xml_kind=node_kind,
            output=_parse_optional(parser, entity.get_attr("output", optional=True)),
            arguments=_parse_optional(parser, entity.get_attr("args", optional=True)),
            respawn=_parse_optional(parser, entity.get_attr("respawn", optional=True)),
            respawn_delay=_parse_optional(parser, entity.get_attr("respawn_delay", optional=True)),
        )

    def __init__(self, *, package=None, executable=None, name=None, **kwargs):
        self._idx = -1  # set lazily in execute()
        self._kind = kwargs.pop("_xml_kind", None) or "node"
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
        self._xml_params = kwargs.get("_xml_params")
        self._xml_remaps = kwargs.get("_xml_remaps")
        self._xml_envs = kwargs.get("_xml_envs")
        self._detailed = False

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
        if not self._detailed:
            self._detailed = True
            if self._xml_params is not None:
                self._resolve_xml_details(context)
            elif context is not None:
                from launch_plus.entities.node_resolution import _resolve_node_details

                _resolve_node_details(state, self, context)
        return None

    def _resolve_xml_details(self, context) -> None:
        """Resolve XML-parsed token structures into the tracked node entry."""
        from launch_plus.entities.helpers import resolve_value

        entry = context._state.tracked["nodes"][self._idx]
        # Package / executable / name
        pkg = resolve_value(self._raw_package, context) or ""
        exe = resolve_value(self._raw_executable, context) or ""
        name = resolve_value(self._raw_name, context) or ""
        ns = resolve_value(self._raw_namespace, context)
        if pkg:
            context._state.track_package(pkg)
        entry["package"] = pkg
        entry["executable"] = exe
        entry["name"] = name
        entry["explicit_namespace"] = ns
        # Read from _launch_configurations (matching official Node._perform_substitutions)
        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns
        # Params — from launch_configurations['global_params']
        ctx_gp = context._launch_configurations.get("global_params", [])
        params: dict[str, str] = {k: str(v) for k, v in ctx_gp}
        # Param files — from launch_configurations['global_param_files']
        param_files: list[dict] = list(context._launch_configurations.get("global_param_files", []))
        for p in self._xml_params:
            if "from" in p:
                path = resolve_value(p["from"], context) or ""
                context._state.track_param_file(path)
                pf_entry: dict = {"path": path}
                if context._state.inline_params:
                    from launch_plus.entities.helpers import _read_and_expand_param_file

                    expanded = _read_and_expand_param_file(path, context)
                    if expanded is not None:
                        pf_entry["params"] = expanded
                param_files.append(pf_entry)
            else:
                k = resolve_value(p["name"], context) or ""
                v = resolve_value(p["value"], context) or ""
                params[k] = v
        entry["parameters"] = params
        entry["param_files"] = param_files
        # Remaps — from launch_configurations['ros_remaps']
        remaps: list = list(context._launch_configurations.get("ros_remaps", []))
        for src_tokens, dst_tokens in self._xml_remaps or []:
            remaps.append(
                [resolve_value(src_tokens, context) or "", resolve_value(dst_tokens, context) or ""]
            )
        entry["remappings"] = remaps
        # Env
        from launch_plus.entities.node_resolution import _env_overrides

        env = _env_overrides(context)
        for k_tokens, v_tokens in self._xml_envs or []:
            env[resolve_value(k_tokens, context) or ""] = resolve_value(v_tokens, context) or ""
        entry["env"] = env
        # Extra fields
        entry["output"] = resolve_value(self._raw_output, context)
        entry["args"] = resolve_value(self._raw_arguments, context)
        entry["respawn"] = resolve_value(self._raw_respawn, context)
        entry["respawn_delay"] = resolve_value(self._raw_respawn_delay, context)

    def __repr__(self):
        return f"TrackedNode(package={self._raw_package!r})"


class _TrackedLifecycleNode(_TrackedNode):
    def __init__(self, **kwargs):
        kwargs.setdefault("_xml_kind", "lifecycle_node")
        super().__init__(**kwargs)
        self._kind = "lifecycle_node"


class _TrackedComposableNode(_TrackedAction):
    """A composable node plugin loaded into a container process.

    Does NOT add to the flat ``_state.tracked["nodes"]`` list — it is attached to the
    container's ``plugins`` list when the container is resolved in ``execute()``.
    """

    def __init__(self, *, package=None, plugin=None, name=None, **kwargs):
        self._raw_package = package
        self._package = str(package) if package else ""
        self._raw_plugin = plugin
        self._plugin = str(plugin) if plugin else ""
        self._raw_name = name
        self._name = str(name) if name else ""
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])

    def __repr__(self):
        return f"TrackedComposableNode(package={self._package!r}, plugin={self._plugin!r})"


@expose_action("node_container")
@expose_action("composable_node_container")
class _TrackedComposableNodeContainer(_TrackedAction):
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
            _xml_envs=parser.parse_envs(entity),
            _xml_plugins=parser.parse_composable_plugins(entity),
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
        self._idx = -1  # set lazily in execute()
        # Pop XML-path parsed data before they leak into kwargs
        xml_envs = kwargs.pop("_xml_envs", None)
        xml_plugins = kwargs.pop("_xml_plugins", None)
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._xml_envs = xml_envs
        self._xml_plugins = xml_plugins
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False

    def _ensure_tracked(self, state) -> int:
        """Create the tracked container entry on first call, return index."""
        if self._idx >= 0:
            return int(self._idx)
        state.track_package(self._raw_package)
        for desc in self._descs:
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
        if not self._detailed:
            self._detailed = True
            if self._xml_plugins is not None:
                self._resolve_xml_details(context)
            elif context is not None:
                from launch_plus.entities.node_resolution import (
                    _resolve_composable_plugins,
                    _resolve_node_details,
                )

                _resolve_node_details(state, self, context)
                context._state.tracked["nodes"][self._idx]["plugins"] = _resolve_composable_plugins(
                    state, self._descs, context
                )
        return None

    def _resolve_xml_details(self, context) -> None:
        """Resolve XML-parsed token structures into the tracked node entry."""
        from launch_plus.entities.helpers import resolve_value

        entry = context._state.tracked["nodes"][self._idx]
        # Package / executable / name
        pkg = resolve_value(self._raw_package, context) or ""
        exe = resolve_value(self._raw_executable, context) or ""
        name = resolve_value(self._raw_name, context) or ""
        ns = resolve_value(self._raw_namespace, context)
        if pkg:
            context._state.track_package(pkg)
        entry["package"] = pkg
        entry["executable"] = exe
        entry["name"] = name
        entry["explicit_namespace"] = ns
        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns
        # Params (container has no inline params from XML — only global)
        ctx_gp = context._launch_configurations.get("global_params", [])
        entry["parameters"] = {k: str(v) for k, v in ctx_gp}
        entry["param_files"] = list(context._launch_configurations.get("global_param_files", []))
        entry["remappings"] = list(context._launch_configurations.get("ros_remaps", []))
        # Env
        from launch_plus.entities.node_resolution import _env_overrides

        env = _env_overrides(context)
        for k_tokens, v_tokens in self._xml_envs or []:
            env[resolve_value(k_tokens, context) or ""] = resolve_value(v_tokens, context) or ""
        entry["env"] = env
        # Plugins
        entry["plugins"] = _resolve_xml_composable_plugins(self._xml_plugins, context)


@expose_action("load_composable_node")
class _TrackedLoadComposableNodes(_TrackedAction):
    """Loads composable nodes into an existing container."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        target_raw = entity.get_attr("target", optional=True)
        ns_raw = entity.get_attr("namespace", optional=True)
        return cls(
            target_container=_parse_optional(parser, target_raw),
            _xml_plugins=parser.parse_composable_plugins(entity),
            _xml_namespace=_parse_optional(parser, ns_raw),
        )

    def __init__(self, *, composable_node_descriptions=None, target_container=None, **kwargs):
        self._idx = -1  # set lazily in execute()
        # Pop XML-path parsed data before they leak into kwargs
        xml_plugins = kwargs.pop("_xml_plugins", None)
        xml_namespace = kwargs.pop("_xml_namespace", None)
        self._raw_target = target_container
        self._xml_plugins = xml_plugins
        self._xml_namespace = xml_namespace
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False

    def _ensure_tracked(self, state) -> int:
        """Create the tracked load_composable entry on first call, return index."""
        if self._idx >= 0:
            return int(self._idx)
        for desc in self._descs:
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
        if not self._detailed:
            self._detailed = True
            if self._xml_plugins is not None:
                self._resolve_xml_details(context)
            elif context is not None:
                entry = context._state.tracked["nodes"][self._idx]
                if self._raw_target is not None:
                    if isinstance(self._raw_target, _TrackedComposableNodeContainer):
                        self._raw_target._ensure_tracked(state)
                        target = context._state.tracked["nodes"][self._raw_target._idx].get(
                            "name"
                        ) or entry.get("target", "")
                    else:
                        target = context.perform_substitution(self._raw_target)
                    if target:
                        entry["target"] = target
                from launch_plus.entities.node_resolution import _resolve_composable_plugins

                entry["plugins"] = _resolve_composable_plugins(state, self._descs, context)
        return None

    def _resolve_xml_details(self, context) -> None:
        """Resolve XML-parsed token structures into the tracked node entry."""
        from launch_plus.entities.helpers import resolve_value

        entry = context._state.tracked["nodes"][self._idx]
        target = resolve_value(self._raw_target, context) or ""
        ns = resolve_value(self._xml_namespace, context)
        entry["target"] = target
        entry["explicit_namespace"] = ns
        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns
        # Plugins
        entry["plugins"] = _resolve_xml_composable_plugins(self._xml_plugins, context)
