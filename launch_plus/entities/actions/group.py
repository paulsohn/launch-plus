"""Action handler for <group> element."""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
)


@expose_action("group")
def _action_group(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    scoped_raw = entity.get_attr("scoped", optional=True)
    if scoped_raw is None:
        scoped = True
    elif isinstance(scoped_raw, bool):
        scoped = scoped_raw
    else:
        scoped = str(scoped_raw).lower() not in ("false", "0", "no")
    children = entity.children
    if scoped:
        saved_args = dict(parser.ctx.args)
        saved_vars = dict(parser.ctx.vars)
        saved_env = dict(_state.env)
        saved_ns_depth = len(_state.namespace_stack)
        saved_gp = list(_state.global_params)
        saved_gr = list(_state.global_remaps)
        saved_gpf = list(_state.global_param_files)
    parser.resolve_children(list(children))
    if scoped:
        new_args = {k: v for k, v in parser.ctx.args.items() if k not in saved_args}
        parser.ctx.args = saved_args
        parser.ctx.args.update(new_args)
        parser.ctx.vars = saved_vars
        _state.env = saved_env
        del _state.namespace_stack[saved_ns_depth:]
        _state.global_params[:] = saved_gp
        _state.global_remaps[:] = saved_gr
        _state.global_param_files[:] = saved_gpf
