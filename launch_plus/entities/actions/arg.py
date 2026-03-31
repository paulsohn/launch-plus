"""Action handler for <arg> element."""

from __future__ import annotations

import logging

from launch_plus.entities.action import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.helpers import _portable_display
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity

logger = logging.getLogger("launch_plus")


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
            default_value=(parser.parse_substitution(default) if default is not None else None),
            _fixed_value=(
                parser.parse_substitution(fixed_value) if fixed_value is not None else None
            ),
        )

    def __init__(self, name=None, *positional, default_value=None, condition=None, **kwargs):
        self.name = str(name) if name is not None else (str(positional[0]) if positional else None)
        self.default_value = default_value
        self.condition = condition
        self._fixed_value = kwargs.get("_fixed_value")

    def execute(self, context) -> list | None:
        from launch_plus.entities.helpers import resolve_value
        from launch_plus.entities.substitutions.launch_config import _DeferredDefault

        name = self.name
        if not name:
            return None

        # Evaluate condition (Python shim path only)
        if self.condition is not None and hasattr(self.condition, "evaluate"):
            try:
                if not self.condition.evaluate(context):
                    return None
            except Exception as e:
                logger.warning(
                    "condition on DeclareLaunchArgument '%s' failed: %s; assuming satisfied",
                    name,
                    e,
                )

        state = context._state

        if self._fixed_value is not None:
            # <arg name="x" value="v"/> — fixed value, set immediately
            resolved = resolve_value(self._fixed_value, context) or ""
            context._launch_configurations[name] = resolved
            _record_and_track(name, resolved, context)
            return None

        # Default value handling
        already_set = name in context._launch_configurations
        if already_set:
            # Arg already provided — record the default display for --show-args
            dv = self.default_value
            if dv is not None:
                display = resolve_value(dv, context) or ""
            else:
                display = context._launch_configurations.get(name, "")
            _record_and_track(name, display, context)
            return None

        if self.default_value is None:
            _record_and_track(name, "", context)
            return None

        if not state.apply_arg_defaults:
            _record_and_track(name, "", context)
            return None

        # Apply default — set in _launch_configurations so $(var name) can find it
        dv = self.default_value
        context._launch_configurations[name] = _DeferredDefault(dv)
        display = resolve_value(dv, context) or ""
        _record_and_track(name, display, context)
        return None


def _record_and_track(name: str, resolved: str, context=None) -> None:
    """Record a declared arg in tracked state."""
    if not name:
        return
    state = context._state
    already_seen = name in state.declared_arg_names
    if not already_seen:
        state.declared_arg_names.add(name)
    state.record_declared_arg(name, resolved, flat=not already_seen)


def _execute_xml_arg(arg: _DeclaredArg, context) -> None:
    """Execute <arg> for the XML path — context is a _SubstitutionContext."""
    from launch_plus.entities.helpers import resolve_value

    name = arg.name or ""
    if not name:
        return
    if arg.default_value is not None:
        if name not in context._launch_configurations:
            if context._state.apply_arg_defaults:
                resolved = resolve_value(arg.default_value, context) or ""
                context._launch_configurations[name] = resolved
            else:
                resolved = ""
        else:
            resolved = str(context._launch_configurations.get(name, ""))
    else:
        resolved = str(context._launch_configurations.get(name, ""))
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
            logger.warning(
                "condition on DeclareLaunchArgument '%s' failed to evaluate: %s; "
                "assuming condition is satisfied",
                arg.name,
                e,
            )

    if arg.default_value is None:
        _record_and_track(arg.name, "", context)
        return

    already_set = context is not None and arg.name in context._launch_configurations
    if already_set:
        dv = arg.default_value
        if isinstance(dv, list):
            raw = "".join(_portable_display(s) for s in dv)
        else:
            raw = _portable_display(dv)
        _record_and_track(arg.name, raw, context)
        return

    if not context._state.apply_arg_defaults:
        _record_and_track(arg.name, "", context)
        return

    dv = arg.default_value
    if isinstance(dv, list):
        display = "".join(_portable_display(s) for s in dv)
    else:
        display = _portable_display(dv)

    _record_and_track(arg.name, display, context)

    if context is not None:
        context._launch_configurations[arg.name] = _DeferredDefault(dv)
