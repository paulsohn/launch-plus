"""Action handler for <arg> element."""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("arg")
class _DeclaredArg(_TrackedAction):
    """Stub for DeclareLaunchArgument / <arg>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        name = entity.get_attr("name", optional=True) or ""
        default = entity.get_attr("default", optional=True)
        fixed_value = entity.get_attr("value", optional=True)
        _ = entity.get_attr("description", optional=True)  # consume
        return cls(
            name=name,
            default_value=parser.parse_substitution(default) if default else None,
            _fixed_value=parser.parse_substitution(fixed_value) if fixed_value else None,
        )

    def __init__(self, name=None, *positional, default_value=None, condition=None, **kwargs):
        self.name = str(name) if name is not None else (str(positional[0]) if positional else None)
        self.default_value = default_value
        self.condition = condition
        self._fixed_value = kwargs.get("_fixed_value")

    def execute(self, context) -> list | None:
        from launch_plus.entities.xml_resolver import resolve_value

        if self._fixed_value is not None:
            # <arg name="x" value="v"/> — fixed value, set immediately
            resolved = resolve_value(self._fixed_value, context) or ""
            if context is not None and hasattr(context, "args"):
                context.args[self.name] = resolved
            _record_and_track(self.name, resolved, context)
        elif hasattr(context, "args"):
            # XML path: resolve default and record
            _execute_xml_arg(self, context)
        elif hasattr(context, "_launch_configurations"):
            # Python shim path
            _apply_declared_arg(self, context)
        return None


def _record_and_track(name: str, resolved: str, context=None) -> None:
    """Record a declared arg in tracked state."""
    if not name:
        return
    state = context._state
    already_seen = name in state.declared_arg_names
    if not already_seen:
        state.declared_arg_names.add(name)
    _R._record_declared_arg(state, name, resolved, flat=not already_seen)


def _execute_xml_arg(arg: _DeclaredArg, context) -> None:
    """Execute <arg> for the XML path — context is a _SubstitutionContext."""
    from launch_plus.entities.xml_resolver import resolve_value

    name = arg.name or ""
    if not name:
        return
    if arg.default_value is not None:
        if name not in context.args:
            if context._state.apply_arg_defaults:
                resolved = resolve_value(arg.default_value, context) or ""
                context.args[name] = resolved
            else:
                resolved = ""
        else:
            resolved = context.args.get(name, "")
    else:
        resolved = context.args.get(name, "")
    _record_and_track(name, resolved, context)


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
            st = context._state
            st.warn(
                f"condition on DeclareLaunchArgument '{arg.name}' failed to evaluate: {e}; "
                f"assuming condition is satisfied"
            )

    if arg.default_value is None:
        _record_and_track(arg.name, "", context)
        return

    already_set = context is not None and arg.name in context._launch_configurations
    if already_set:
        dv = arg.default_value
        if isinstance(dv, list):
            raw = "".join(_R._portable_display(s) for s in dv)
        else:
            raw = _R._portable_display(dv)
        _record_and_track(arg.name, raw, context)
        return

    if not context._state.apply_arg_defaults:
        _record_and_track(arg.name, "", context)
        return

    dv = arg.default_value
    if isinstance(dv, list):
        display = "".join(_R._portable_display(s) for s in dv)
    else:
        display = _R._portable_display(dv)

    _record_and_track(arg.name, display, context)

    if context is not None:
        context._launch_configurations[arg.name] = _DeferredDefault(dv)
