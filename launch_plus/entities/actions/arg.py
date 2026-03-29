"""Action handlers for <arg> and <let> elements."""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _record_declared_arg,
    _state,
)


@expose_action("arg")
def _action_arg(entity: Entity, parser: _ActionParser) -> None:
    name = entity.get_attr("name", optional=True) or ""
    default = entity.get_attr("default", optional=True)
    fixed_value = entity.get_attr("value", optional=True)
    _ = entity.get_attr("description", optional=True)  # consume
    ctx = parser.ctx
    if fixed_value is not None:
        resolved = parser.resolve(fixed_value)
        ctx.args[name] = resolved
    elif name and name not in ctx.args and default is not None:
        if _state.apply_arg_defaults:
            resolved = parser.resolve(default)
            ctx.args[name] = resolved
        else:
            resolved = default or ""
    else:
        resolved = ctx.args.get(name, default or "")
    if name:
        already_seen = name in _state.declared_arg_names
        if not already_seen:
            _state.declared_arg_names.add(name)
        _record_declared_arg(name, resolved, flat=not already_seen)


@expose_action("let")
def _action_let(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = entity.get_attr("name", optional=True) or ""
        value = parser.resolve(entity.get_attr("value", optional=True) or "")
        parser.ctx.vars[name] = value
