"""Action handlers for node-related elements.

Covers: <node>, <lifecycle_node>, <node_container>,
<composable_node_container>, <load_composable_node>.
"""

from __future__ import annotations

from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity


def _parse_optional(parser: _ActionParser, text: str | None) -> list | None:
    """Parse an optional attribute to tokens, or return None."""
    if text is None:
        return None
    return parser.parse_substitution(text)


def _fully_qualified_name(node_entry: dict) -> str:
    """Compute fully-qualified node name from tracked entry.

    Matches official ``prefix_namespace(ros_namespace, node_namespace) + "/" + name``
    then ``make_namespace_absolute``.
    """
    from launch_plus.entities.helpers import _ros2_namespace_join

    ros_ns = node_entry.get("ros_namespace")
    explicit_ns = node_entry.get("explicit_namespace")
    name = node_entry.get("name", "")
    # Combine ros_namespace + explicit_namespace
    ns = _ros2_namespace_join(ros_ns, explicit_ns) if explicit_ns else ros_ns
    if ns and name:
        return f"{ns.rstrip('/')}/{name}"
    if ns:
        return ns
    if name:
        return f"/{name}" if not name.startswith("/") else name
    return ""


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
class Node(Action):
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
        self._idx = -1
        self._kind = kwargs.pop("_xml_kind", None) or "node"
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        # Unified param/remap/env: XML parse stores these via _xml_* kwargs,
        # Python shim stores via parameters/remappings/env kwargs.
        # Merge both into single fields.
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._raw_output = kwargs.get("output")
        self._raw_arguments = kwargs.get("arguments")
        self._raw_respawn = kwargs.get("respawn")
        self._raw_respawn_delay = kwargs.get("respawn_delay")
        # XML-parsed token structures (merged into the same fields for execute)
        self._xml_params = kwargs.get("_xml_params")
        self._xml_remaps = kwargs.get("_xml_remaps")
        self._xml_envs = kwargs.get("_xml_envs")
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
        return None

    def _perform_substitutions(self, context) -> None:
        """Resolve all substitutions into the tracked entry.

        Unified path for both XML parse and Python shim — matching the
        official ``Node._perform_substitutions(context)`` pattern.
        """
        from launch_plus.entities.helpers import (
            _read_and_expand_param_file,
            env_overrides,
            resolve_value,
        )
        from launch_plus.entities.substitution import Substitution

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

        # Parameters: global first, then node-specific (matching official order)
        ctx_gp = context._launch_configurations.get("global_params", [])
        params: dict[str, str] = {k: str(v) for k, v in ctx_gp}
        global_pf = list(context._launch_configurations.get("global_param_files", []))
        pf_list: list[dict] = list(global_pf)
        seen_pf: set[str] = set()

        # XML-parsed params
        for p in self._xml_params or []:
            if "from" in p:
                path = resolve_value(p["from"], context) or ""
                state.track_param_file(path)
                pf_entry: dict = {"path": path}
                if state.inline_params:
                    expanded = _read_and_expand_param_file(path, context)
                    if expanded is not None:
                        pf_entry["params"] = expanded
                pf_list.append(pf_entry)
            else:
                k = resolve_value(p["name"], context) or ""
                v = resolve_value(p["value"], context) or ""
                params[k] = v

        # Python shim params (ParameterFile objects and dicts)
        for p in self._raw_parameters:
            if hasattr(p, "_param_file"):
                path = p._param_file
                if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                    raw = p._raw_param_file
                    if isinstance(raw, Substitution):
                        try:
                            result = raw.perform(context)
                            if result is not None:
                                path = str(result)
                        except Exception:
                            pass
                    elif not isinstance(raw, str):
                        path = str(raw)
                if path:
                    path = str(path)
                    if path not in seen_pf:
                        seen_pf.add(path)
                        pf_entry = {"path": path}
                        if state.inline_params:
                            expanded = _read_and_expand_param_file(path, state=state)
                            if expanded is not None:
                                pf_entry["params"] = expanded
                        pf_list.append(pf_entry)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    params[str(k)] = resolved_v if resolved_v is not None else ""

        entry["parameters"] = params
        entry["param_files"] = pf_list

        # Remappings: global first, then node-specific (matching official)
        remaps = list(context._launch_configurations.get("ros_remaps", []))
        for src_tokens, dst_tokens in self._xml_remaps or []:
            remaps.append(
                [resolve_value(src_tokens, context) or "", resolve_value(dst_tokens, context) or ""]
            )
        for r in self._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                remaps.append([src or str(r[0]), dst or str(r[1])])
        entry["remappings"] = remaps

        # Environment
        env = env_overrides(context)
        for k_tokens, v_tokens in self._xml_envs or []:
            env[resolve_value(k_tokens, context) or ""] = resolve_value(v_tokens, context) or ""
        raw_env = self._raw_env
        if isinstance(raw_env, dict):
            for k, v in raw_env.items():
                env[context.perform_substitution(k) or str(k)] = (
                    context.perform_substitution(v) or ""
                )
        elif isinstance(raw_env, (list, tuple)):
            for item in raw_env:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    env[context.perform_substitution(item[0]) or str(item[0])] = (
                        context.perform_substitution(item[1]) or ""
                    )
        entry["env"] = env

        # Extra fields
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

    def __repr__(self):
        return f"TrackedNode(package={self._raw_package!r})"


class LifecycleNode(Node):
    def __init__(self, **kwargs):
        kwargs.setdefault("_xml_kind", "lifecycle_node")
        super().__init__(**kwargs)
        self._kind = "lifecycle_node"


class ComposableNode(Action):
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
        from launch_plus.entities.helpers import env_overrides

        env = env_overrides(context)
        for k_tokens, v_tokens in self._xml_envs or []:
            env[resolve_value(k_tokens, context) or ""] = resolve_value(v_tokens, context) or ""
        entry["env"] = env
        # Plugins
        entry["plugins"] = _resolve_xml_composable_plugins(self._xml_plugins, context)


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
                    if isinstance(self._raw_target, ComposableNodeContainer):
                        self._raw_target._ensure_tracked(state)
                        container = context._state.tracked["nodes"][self._raw_target._idx]
                        target = _fully_qualified_name(container)
                    else:
                        target = context.perform_substitution(self._raw_target)
                    if target:
                        entry["target"] = target
                ros_ns = context._launch_configurations.get("ros_namespace")
                if ros_ns:
                    entry["ros_namespace"] = ros_ns
                # _resolve_composable_plugins is defined below in this file

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


# ─── Node detail resolution (moved from node_resolution.py) ──────────────────


def _resolve_node_details(state, node, context):
    """Fill in deferred details (package, executable, name, namespace, params, remaps, env).

    Works for both ``Node`` / ``LifecycleNode`` and
    ``ComposableNodeContainer`` — both expose the same raw fields.
    """
    from launch_plus.entities.helpers import (
        _is_substitution,
        _read_and_expand_param_file,
        env_overrides,
    )
    from launch_plus.entities.substitution import Substitution

    entry = state.tracked["nodes"][node._idx]

    for field_name in ("package", "executable", "name"):
        raw = getattr(node, f"_raw_{field_name}", None)
        if _is_substitution(raw):
            resolved, is_fallback = context.perform_substitution_ex(raw)
            if resolved is not None:
                entry[field_name] = resolved
                if field_name == "package" and not is_fallback:
                    state.track_package(resolved)

    ns = (
        context.perform_substitution(node._raw_namespace)
        if node._raw_namespace is not None
        else None
    )
    entry["explicit_namespace"] = ns
    ros_ns = context._launch_configurations.get("ros_namespace")
    if ros_ns:
        entry["ros_namespace"] = ros_ns

    params = {}
    pf_list: list[dict] = []
    seen_pf: set[str] = set()
    for p in node._raw_parameters:
        if hasattr(p, "_param_file"):
            path = p._param_file
            if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                raw = p._raw_param_file
                if isinstance(raw, Substitution):
                    try:
                        result = raw.perform(context)
                        if result is not None:
                            path = str(result)
                    except Exception:
                        pass
                elif not isinstance(raw, str):
                    path = str(raw)
            if path:
                path = str(path)
                if path not in seen_pf:
                    seen_pf.add(path)
                    pf_entry: dict = {"path": path}
                    if state.inline_params:
                        expanded = _read_and_expand_param_file(path, state=state)
                        if expanded is not None:
                            pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
        elif isinstance(p, dict):
            for k, v in p.items():
                resolved_v = context.perform_substitution(v)
                params[str(k)] = resolved_v if resolved_v is not None else ""
    ctx_global_params = context._launch_configurations.get("global_params", [])
    merged_params = {k: str(v) for k, v in ctx_global_params}
    merged_params.update(params)
    entry["parameters"] = merged_params
    global_pf = context._launch_configurations.get("global_param_files", [])
    entry["param_files"] = list(global_pf) + pf_list

    remaps = list(context._launch_configurations.get("ros_remaps", []))
    for r in node._raw_remappings:
        if isinstance(r, (tuple, list)) and len(r) == 2:
            src = context.perform_substitution(r[0])
            dst = context.perform_substitution(r[1])
            remaps.append([src or str(r[0]), dst or str(r[1])])
    entry["remappings"] = remaps

    env = env_overrides(context)
    raw_env = node._raw_env
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            env[context.perform_substitution(k) or str(k)] = context.perform_substitution(v) or ""
    elif isinstance(raw_env, (list, tuple)):
        for item in raw_env:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                k_str = context.perform_substitution(item[0]) or str(item[0])
                v_str = context.perform_substitution(item[1]) or ""
                env[k_str] = v_str
    entry["env"] = env

    for attr, key in (
        ("_raw_output", "output"),
        ("_raw_arguments", "args"),
        ("_raw_respawn", "respawn"),
        ("_raw_respawn_delay", "respawn_delay"),
    ):
        raw = getattr(node, attr, None)
        if raw is not None:
            resolved = context.perform_substitution(raw)
            if resolved is not None:
                entry[key] = resolved
            elif isinstance(raw, str):
                entry[key] = raw
            else:
                entry[key] = str(raw)


def _resolve_composable_plugins(state, descs, context):
    """Convert ``ComposableNode`` descriptions to serialisable plugin dicts."""
    from launch_plus.entities.helpers import (
        _is_substitution,
        _read_and_expand_param_file,
    )
    from launch_plus.entities.substitution import Substitution

    plugins = []
    for desc in descs:
        if not isinstance(desc, ComposableNode):
            pkg = getattr(desc, "package", None) or getattr(desc, "_package", None)
            plugin = getattr(desc, "plugin", None) or getattr(desc, "_plugin", None)
            if pkg or plugin:
                plugins.append(
                    {
                        "package": str(pkg) if pkg else "",
                        "plugin": str(plugin) if plugin else "",
                        "name": None,
                        "parameters": {},
                        "remappings": [],
                    }
                )
            continue
        params = {}
        pf_list: list[dict] = []
        seen_pf: set[str] = set()
        for p in desc._raw_parameters:
            if hasattr(p, "_param_file"):
                path = p._param_file
                if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                    raw = p._raw_param_file
                    if isinstance(raw, Substitution):
                        try:
                            result = raw.perform(context)
                            if result is not None:
                                path = str(result)
                        except Exception:
                            pass
                    elif not isinstance(raw, str):
                        path = str(raw)
                if path:
                    path = str(path)
                    if path not in seen_pf:
                        seen_pf.add(path)
                        pf_entry: dict = {"path": path}
                        if state.inline_params:
                            expanded = _read_and_expand_param_file(path, state=state)
                            if expanded is not None:
                                pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    params[str(k)] = resolved_v if resolved_v is not None else ""
        remaps = []
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
        pkg = desc._package
        if _is_substitution(desc._raw_package):
            resolved_pkg, is_fallback = context.perform_substitution_ex(desc._raw_package)
            if resolved_pkg is not None:
                pkg = resolved_pkg
                if not is_fallback:
                    state.track_package(resolved_pkg)
        plg = desc._plugin
        if _is_substitution(desc._raw_plugin):
            resolved_plg = context.perform_substitution(desc._raw_plugin)
            if resolved_plg is not None:
                plg = resolved_plg
        nm = desc._name
        if _is_substitution(desc._raw_name):
            resolved_nm = context.perform_substitution(desc._raw_name)
            if resolved_nm is not None:
                nm = resolved_nm
        plugins.append(
            {
                "package": pkg,
                "plugin": plg,
                "name": nm or None,
                "parameters": params,
                "remappings": remaps,
                "param_files": pf_list,
            }
        )
    return plugins
