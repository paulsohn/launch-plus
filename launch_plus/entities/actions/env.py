"""Action handlers for environment and configuration elements.

Covers: <set_env>, <unset_env>, <push-ros-namespace>,
<set_parameter>, <set_remap>.
"""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
)


@expose_action("set_env")
def _action_set_env(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = parser.resolve(entity.get_attr("name", optional=True) or "")
        value = parser.resolve(entity.get_attr("value", optional=True) or "")
        _state.env[name] = value
        parser.ctx.env[name] = value


@expose_action("unset_env")
def _action_unset_env(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = parser.resolve(entity.get_attr("name", optional=True) or "")
        _state.env.pop(name, None)
        parser.ctx.env.pop(name, None)


@expose_action("push-ros-namespace")
def _action_push_ros_namespace(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        ns = parser.resolve(entity.get_attr("namespace", optional=True) or "")
        if ns:
            _state.namespace_stack.append(ns)


@expose_action("set_parameter")
def _action_set_parameter(entity: Entity, parser: _ActionParser) -> None:
    name = parser.resolve(entity.get_attr("name", optional=True) or "")
    value = parser.resolve(entity.get_attr("value", optional=True) or "")
    _state.tracked["global_params"].append([name, value])
    _state.global_params.append((name, value))


@expose_action("set_remap")
def _action_set_remap(entity: Entity, parser: _ActionParser) -> None:
    src = parser.resolve(entity.get_attr("from", optional=True) or "")
    dst = parser.resolve(entity.get_attr("to", optional=True) or "")
    _state.global_remaps.append((src, dst))
