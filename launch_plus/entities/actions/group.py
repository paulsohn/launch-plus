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


# ─── Python-shim actions ─────────────────────────────────────────────────────

import launch_plus.resolver as _R  # noqa: E402
from launch_plus.entities.actions.base import _TrackedAction  # noqa: E402
from launch_plus.entities.state import (  # noqa: E402
    _error,
    _PackageNotFetchedError,
    _warn,
)


class _TrackedOpaqueFunction(_TrackedAction):
    """Stores an OpaqueFunction's callable so the walker can invoke it."""

    def __init__(self, *, function=None, **kwargs):
        self.function = function

    def execute(self, context) -> list | None:
        fn = self.function
        if fn and context:
            try:
                result = _R._call_opaque_with_stubs(fn, context)
                return result if result else None
            except _PackageNotFetchedError as e:
                _error(f"OpaqueFunction failed: package fetch failed: {e}")
            except Exception as e:
                _error(f"OpaqueFunction failed: {e}")
        return None


class _TrackedGroupAction(_TrackedAction):
    """Stores GroupAction's child actions so the walker can recurse into them."""

    def __init__(self, actions=None, **kwargs):
        self._actions = list(actions or [])
        self._scoped = kwargs.get("scoped", True)
        self._condition = kwargs.get("condition")

    def execute(self, context) -> list | None:
        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"GroupAction condition evaluation failed: {e}")
                return None
        depth_before = len(_R._state.namespace_stack)
        saved_env = dict(_R._state.env) if self._scoped else None
        _R._walk_actions(self._actions, context)
        del _R._state.namespace_stack[depth_before:]
        if saved_env is not None:
            _R._state.env.clear()
            _R._state.env.update(saved_env)
        return None


class _TimerAction(_TrackedAction):
    """Stores TimerAction child actions so the walker can recurse into them."""

    def __init__(self, *, period=None, actions=None, **kwargs):
        self._actions = list(actions or [])

    def execute(self, context) -> list | None:
        return self._actions or None
