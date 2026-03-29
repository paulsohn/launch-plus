"""Action handlers for node-related elements.

Covers: <node>, <lifecycle_node>, <node_container>,
<composable_node_container>, <load_composable_node>.
"""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
    _track_package,
)


@expose_action("node")
@expose_action("lifecycle_node")
def _action_node(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    pkg = parser.resolve(
        entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
    )
    exe = parser.resolve(
        entity.get_attr("exec", optional=True) or entity.get_attr("executable", optional=True) or ""
    )
    name = parser.resolve_optional(entity.get_attr("name", optional=True))
    ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    _track_package(pkg)
    params, param_files = parser.resolve_params(entity)
    remaps = parser.resolve_remaps(entity)
    env = dict(_state.env)
    env.update(parser.resolve_envs(entity))
    merged_params = {k: str(v) for k, v in _state.global_params}
    merged_params.update(params)
    merged_param_files = list(_state.global_param_files) + param_files
    merged_remaps = list(_state.global_remaps) + remaps
    node_kind = "node" if entity.type_name != "lifecycle_node" else "lifecycle_node"
    parser.track_node(
        {
            "package": pkg,
            "executable": exe,
            "name": name or "",
            "namespace_stack": list(_state.namespace_stack),
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


@expose_action("node_container")
@expose_action("composable_node_container")
def _action_node_container(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    pkg = parser.resolve(
        entity.get_attr("pkg", optional=True) or entity.get_attr("package", optional=True) or ""
    )
    exe = parser.resolve(
        entity.get_attr("exec", optional=True) or entity.get_attr("executable", optional=True) or ""
    )
    name = parser.resolve_optional(entity.get_attr("name", optional=True))
    ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    _track_package(pkg)
    env = dict(_state.env)
    env.update(parser.resolve_envs(entity))
    plugins = parser.resolve_composable_plugins(entity)
    parser.track_node(
        {
            "package": pkg,
            "executable": exe,
            "name": name or "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": ns,
            "parameters": {k: str(v) for k, v in _state.global_params},
            "param_files": list(_state.global_param_files),
            "remappings": list(_state.global_remaps),
            "env": env,
            "kind": "container",
            "plugins": plugins,
            "target": None,
        }
    )


@expose_action("load_composable_node")
def _action_load_composable_node(entity: Entity, parser: _ActionParser) -> None:
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
            "namespace_stack": list(_state.namespace_stack),
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
