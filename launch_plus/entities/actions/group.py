"""Action handler for <group> element."""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("group")
class _TrackedGroupAction(_TrackedAction):
    """Stores GroupAction's child actions so the walker can recurse into them."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        scoped_raw = entity.get_attr("scoped", optional=True)
        if scoped_raw is None:
            scoped = True
        elif isinstance(scoped_raw, bool):
            scoped = scoped_raw
        else:
            scoped = str(scoped_raw).lower() not in ("false", "0", "no")
        return cls(
            scoped=scoped,
            _xml_children=list(entity.children),
            _xml_include_stack=list(parser.include_stack),
            _xml_ctx=parser.ctx,
        )

    def __init__(self, actions=None, **kwargs):
        self._actions = list(actions or [])
        self._scoped = kwargs.get("scoped", True)
        self._condition = kwargs.get("condition")
        # XML path: child entities to resolve in execute()
        self._xml_children = kwargs.get("_xml_children")
        self._xml_include_stack = kwargs.get("_xml_include_stack")
        self._xml_ctx = kwargs.get("_xml_ctx")

    def execute(self, context) -> list | None:
        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except Exception as e:
                context._state.warn(f"GroupAction condition evaluation failed: {e}")
                return None
        if self._xml_children is not None:
            self._execute_xml(context)
        else:
            self._execute_shim(context)
        return None

    def _execute_xml(self, ctx) -> None:
        """Execute for XML path: resolve child entities with scoping."""
        from launch_plus.entities.xml_resolver import _resolve_element

        if self._scoped:
            saved_args = dict(ctx.args)
            saved_vars = dict(ctx.vars)
            saved_env = dict(ctx._state.env)
            saved_ns_depth = len(ctx._state.namespace_stack)
            saved_gp = list(ctx._state.global_params)
            saved_gr = list(ctx._state.global_remaps)
            saved_gpf = list(ctx._state.global_param_files)
        for child in self._xml_children:
            _resolve_element(child, ctx, self._xml_include_stack or [])
        if self._scoped:
            new_args = {k: v for k, v in ctx.args.items() if k not in saved_args}
            ctx.args = saved_args
            ctx.args.update(new_args)
            ctx.vars = saved_vars
            ctx._state.env.clear()
            ctx._state.env.update(saved_env)
            del ctx._state.namespace_stack[saved_ns_depth:]
            ctx._state.global_params[:] = saved_gp
            ctx._state.global_remaps[:] = saved_gr
            ctx._state.global_param_files[:] = saved_gpf

    def _execute_shim(self, context) -> None:
        """Execute for Python shim path: walk child actions."""
        depth_before = len(context._state.namespace_stack)
        saved_env = dict(context._state.env) if self._scoped else None
        _R._walk_actions(context._state, self._actions, context)
        del context._state.namespace_stack[depth_before:]
        if saved_env is not None:
            context._state.env.clear()
            context._state.env.update(saved_env)


class _TrackedOpaqueFunction(_TrackedAction):
    """Stores an OpaqueFunction's callable so the walker can invoke it."""

    def __init__(self, *, function=None, **kwargs):
        self.function = function

    def execute(self, context) -> list | None:
        fn = self.function
        if fn and context:
            state = context._state
            try:
                result = _R._call_opaque_with_stubs(state, fn, context)
                return result if result else None
            except Exception as e:
                state.error(f"OpaqueFunction failed: {e}")
        return None


class _TimerAction(_TrackedAction):
    """Stores TimerAction child actions so the walker can recurse into them."""

    def __init__(self, *, period=None, actions=None, **kwargs):
        self._actions = list(actions or [])

    def execute(self, context) -> list | None:
        return self._actions or None
