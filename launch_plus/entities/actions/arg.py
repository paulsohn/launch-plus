"""Action handler for <arg> element."""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.state import (
    _warn,
)
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("arg")
class _DeclaredArg(_TrackedAction):
    """Stub for DeclareLaunchArgument: captures name, default_value, and condition."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser) -> None:
        name = entity.get_attr("name", optional=True) or ""
        default = entity.get_attr("default", optional=True)
        fixed_value = entity.get_attr("value", optional=True)
        _ = entity.get_attr("description", optional=True)  # consume
        ctx = parser.ctx
        if fixed_value is not None:
            resolved = parser.resolve(fixed_value)
            ctx.args[name] = resolved
        elif name and name not in ctx.args and default is not None:
            if parser.apply_arg_defaults:
                resolved = parser.resolve(default)
                ctx.args[name] = resolved
            else:
                resolved = default or ""
        else:
            resolved = ctx.args.get(name, default or "")
        if name:
            already_seen = name in parser.declared_arg_names
            if not already_seen:
                parser.declared_arg_names.add(name)
            parser.record_declared_arg(name, resolved, flat=not already_seen)

    def __init__(self, name=None, *positional, default_value=None, condition=None, **kwargs):
        self.name = str(name) if name is not None else (str(positional[0]) if positional else None)
        self.default_value = default_value
        self.condition = condition

    def execute(self, context) -> list | None:
        _apply_declared_arg(self, context)
        return None


def _apply_declared_arg(arg: _DeclaredArg, context) -> None:
    """Resolve a DeclareLaunchArgument default and apply it to the launch context."""
    from launch_plus.entities.substitutions.launch_config import _DeferredDefault

    if not arg.name:
        return

    if arg.condition is not None and hasattr(arg.condition, "evaluate"):
        try:
            if not arg.condition.evaluate(context):
                return
        except Exception as e:
            _warn(
                f"condition on DeclareLaunchArgument '{arg.name}' failed to evaluate: {e}; "
                f"assuming condition is satisfied"
            )

    if arg.default_value is None:
        already_seen = arg.name in _R._state.declared_arg_names
        if not already_seen:
            _R._state.declared_arg_names.add(arg.name)
        _R._record_declared_arg(arg.name, "", flat=not already_seen)
        return

    already_set = context is not None and arg.name in context._launch_configurations
    if already_set:
        dv = arg.default_value
        if isinstance(dv, list):
            raw = "".join(_R._portable_display(s) for s in dv)
        else:
            raw = _R._portable_display(dv)
        already_seen = arg.name in _R._state.declared_arg_names
        if not already_seen:
            _R._state.declared_arg_names.add(arg.name)
        _R._record_declared_arg(arg.name, raw, flat=not already_seen)
        return

    if not _R._state.apply_arg_defaults:
        _R._record_declared_arg(arg.name, "", flat=arg.name not in _R._state.declared_arg_names)
        if arg.name not in _R._state.declared_arg_names:
            _R._state.declared_arg_names.add(arg.name)
        return

    dv = arg.default_value
    if isinstance(dv, list):
        display = "".join(_R._portable_display(s) for s in dv)
    else:
        display = _R._portable_display(dv)

    already_seen = arg.name in _R._state.declared_arg_names
    if not already_seen:
        _R._state.declared_arg_names.add(arg.name)
    _R._record_declared_arg(arg.name, display, flat=not already_seen)

    if context is not None:
        context._launch_configurations[arg.name] = _DeferredDefault(dv)
