"""Action handlers for node-related elements.

Covers: <node>, <lifecycle_node>, <node_container>,
<composable_node_container>, <load_composable_node>.
"""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("node")
@expose_action("lifecycle_node")
class _TrackedNode(_TrackedAction):
    """Tracks a Node / LifecycleNode for both XML and Python shim paths."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser) -> None:
        if not parser.evaluate_condition(entity):
            return
        pkg = parser.resolve(
            entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
        )
        exe = parser.resolve(
            entity.get_attr("exec", optional=True)
            or entity.get_attr("executable", optional=True)
            or ""
        )
        name = parser.resolve_optional(entity.get_attr("name", optional=True))
        ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
        parser.track_package(pkg)
        params, param_files = parser.resolve_params(entity)
        remaps = parser.resolve_remaps(entity)
        env = dict(parser.env)
        env.update(parser.resolve_envs(entity))
        merged_params = {k: str(v) for k, v in parser.global_params}
        merged_params.update(params)
        merged_param_files = list(parser.global_param_files) + param_files
        merged_remaps = list(parser.global_remaps) + remaps
        node_kind = "node" if entity.type_name != "lifecycle_node" else "lifecycle_node"
        parser.track_node(
            {
                "package": pkg,
                "executable": exe,
                "name": name or "",
                "namespace_stack": list(parser.namespace_stack),
                "explicit_namespace": ns,
                "parameters": merged_params,
                "param_files": merged_param_files,
                "remappings": merged_remaps,
                "env": env,
                "kind": node_kind,
                "plugins": [],
                "target": None,
                "output": parser.resolve_optional(entity.get_attr("output", optional=True)),
                "args": parser.resolve_optional(entity.get_attr("args", optional=True)),
                "respawn": parser.resolve_optional(entity.get_attr("respawn", optional=True)),
                "respawn_delay": parser.resolve_optional(
                    entity.get_attr("respawn_delay", optional=True)
                ),
            }
        )

    def __init__(self, *, package=None, executable=None, name=None, **kwargs):
        _R._track_package(package)
        self._idx = _R._track_node(
            {
                "package": str(package) if package else "",
                "executable": str(executable) if executable else "",
                "name": str(name) if name else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "node",
                "plugins": [],
                "target": None,
            }
        )
        # Save raw kwargs for deferred resolution in execute()
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
        self._detailed = False
        # Eager: track any ParameterFile paths identifiable at construction time
        for p in self._raw_parameters:
            if hasattr(p, "_param_file") and p._param_file:
                _R._track_param_file(p._param_file)

    def execute(self, context) -> list | None:
        if context is not None and not self._detailed:
            self._detailed = True
            _R._resolve_node_details(self, context)
        return None

    def __repr__(self):
        return f"TrackedNode(package={_R._state.tracked['nodes'][self._idx]['package']!r})"


class _TrackedLifecycleNode(_TrackedNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        _R._state.tracked["nodes"][self._idx]["kind"] = "lifecycle_node"


class _TrackedComposableNode(_TrackedAction):
    """A composable node plugin loaded into a container process.

    Does NOT add to the flat ``_state.tracked["nodes"]`` list — it is attached to the
    container's ``plugins`` list when the container is resolved in ``execute()``.
    """

    def __init__(self, *, package=None, plugin=None, name=None, **kwargs):
        _R._track_package(package)
        self._raw_package = package
        self._package = str(package) if package else ""
        self._raw_plugin = plugin
        self._plugin = str(plugin) if plugin else ""
        self._raw_name = name
        self._name = str(name) if name else ""
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        # Eager: track any ParameterFile paths
        for p in self._raw_parameters:
            if hasattr(p, "_param_file") and p._param_file:
                _R._track_param_file(p._param_file)

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
    def parse(cls, entity: Entity, parser: _ActionParser) -> None:
        if not parser.evaluate_condition(entity):
            return
        pkg = parser.resolve(
            entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
        )
        exe = parser.resolve(
            entity.get_attr("exec", optional=True)
            or entity.get_attr("executable", optional=True)
            or ""
        )
        name = parser.resolve_optional(entity.get_attr("name", optional=True))
        ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
        parser.track_package(pkg)
        env = dict(parser.env)
        env.update(parser.resolve_envs(entity))
        plugins = parser.resolve_composable_plugins(entity)
        parser.track_node(
            {
                "package": pkg,
                "executable": exe,
                "name": name or "",
                "namespace_stack": list(parser.namespace_stack),
                "explicit_namespace": ns,
                "parameters": {k: str(v) for k, v in parser.global_params},
                "param_files": list(parser.global_param_files),
                "remappings": list(parser.global_remaps),
                "env": env,
                "kind": "container",
                "plugins": plugins,
                "target": None,
            }
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
        _R._track_package(package)
        self._idx = _R._track_node(
            {
                "package": str(package) if package else "",
                "executable": str(executable) if executable else "",
                "name": str(name) if name else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "container",
                "plugins": [],
                "target": None,
            }
        )
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False
        for desc in self._descs:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                _R._track_package(raw_pkg)

    def execute(self, context) -> list | None:
        if context is not None and not self._detailed:
            self._detailed = True
            _R._resolve_node_details(self, context)
            _R._state.tracked["nodes"][self._idx]["plugins"] = _R._resolve_composable_plugins(
                self._descs, context
            )
        return None


@expose_action("load_composable_node")
class _TrackedLoadComposableNodes(_TrackedAction):
    """Loads composable nodes into an existing container."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser) -> None:
        if not parser.evaluate_condition(entity):
            return
        target = parser.resolve_optional(entity.get_attr("target", optional=True))
        ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
        plugins = parser.resolve_composable_plugins(entity)
        parser.track_node(
            {
                "package": "",
                "executable": "",
                "name": "",
                "namespace_stack": list(parser.namespace_stack),
                "explicit_namespace": ns,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "load_composable",
                "plugins": plugins,
                "target": target or "",
            }
        )

    def __init__(self, *, composable_node_descriptions=None, target_container=None, **kwargs):
        from launch_plus.entities.state import _StubLaunchContext

        if target_container is None:
            target_str = ""
        elif isinstance(target_container, str):
            target_str = target_container
        elif isinstance(target_container, _TrackedComposableNodeContainer):
            target_str = _R._state.tracked["nodes"][target_container._idx].get("name", "")
        elif hasattr(target_container, "perform"):
            try:
                result = target_container.perform(_StubLaunchContext())
                target_str = str(result) if result is not None else str(target_container)
            except Exception:
                target_str = str(target_container)
        else:
            target_str = str(target_container)
        self._raw_target = target_container
        self._idx = _R._track_node(
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
                "target": target_str,
            }
        )
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False
        for desc in self._descs:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                _R._track_package(raw_pkg)

    def execute(self, context) -> list | None:
        if context is not None and not self._detailed:
            self._detailed = True
            entry = _R._state.tracked["nodes"][self._idx]
            if self._raw_target is not None:
                if isinstance(self._raw_target, _TrackedComposableNodeContainer):
                    target = _R._state.tracked["nodes"][self._raw_target._idx].get(
                        "name"
                    ) or entry.get("target", "")
                else:
                    target = _R._resolve_substitution(self._raw_target, context)
                if target:
                    entry["target"] = target
            entry["plugins"] = _R._resolve_composable_plugins(self._descs, context)
        return None
