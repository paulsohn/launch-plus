"""Action handler for <arg> element."""

from __future__ import annotations

import logging

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("arg")
class DeclareLaunchArgument(Action):
    """Stub for DeclareLaunchArgument / <arg>.

    Matching official ``DeclareLaunchArgument.execute()``:
    - If the argument is already set (passed from a parent include), record it for
      ``--show-args`` and leave the value unchanged.
    - If the argument is not set and a default exists, resolve and store the default
      immediately (no deferred resolution).
    - If the argument is not set and has no default, record it as empty (static
      analysis cannot raise; the error would appear at runtime).
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["name"] = entity.get_attr("name", optional=True) or ""
        default = entity.get_attr("default", optional=True)
        fixed_value = entity.get_attr("value", optional=True)
        _ = entity.get_attr("description", optional=True)  # consume
        # choice= child elements — matching official DeclareLaunchArgument.parse()
        # which reads entity.get_attr('choice', data_type=List[Entity], optional=True).
        # Choices are stored for completeness; not used in static analysis.
        choices = entity.get_attr("choice", data_type=list, optional=True)
        if choices is not None:
            kwargs["choices"] = [c.get_attr("value", optional=True) for c in choices]
        if default is not None:
            kwargs["default_value"] = parser.parse_substitution(default)
        if fixed_value is not None:
            kwargs["_fixed_value"] = parser.parse_substitution(fixed_value)
        return cls, kwargs

    def __init__(self, name=None, *positional, default_value=None, choices=None, **kwargs):
        super().__init__(**kwargs)
        self.name = str(name) if name is not None else (str(positional[0]) if positional else None)
        self.default_value = default_value
        self.choices = choices
        self._fixed_value = kwargs.get("_fixed_value")

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import resolve_value

        name = self.name
        if not name:
            return None

        if self._fixed_value is not None:
            # <arg name="x" value="v"/> — fixed value, set immediately
            resolved = resolve_value(self._fixed_value, context) or ""
            context._launch_configurations[name] = resolved
            _record_and_track(name, resolved, context)
            return None

        # Matching official DeclareLaunchArgument.execute():
        # if already set (passed by parent include), leave unchanged.
        # For --show-args display, record the declared default (not the passed value);
        # the passed value already appears in the include args comment from _wrap_with_markers.
        if name in context._launch_configurations:
            dv = self.default_value
            display = resolve_value(dv, context) or "" if dv is not None else ""
            _record_and_track(name, display, context)
            return None

        # Not set — apply default immediately (matching official: no deferred resolution).
        if self.default_value is None:
            # No default and not set: at runtime this would raise; for static analysis
            # record as empty so --show-args can report the argument.
            _record_and_track(name, "", context)
            return None

        resolved = resolve_value(self.default_value, context) or ""
        context._launch_configurations[name] = resolved
        _record_and_track(name, resolved, context)
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


def _apply_declared_arg(arg: DeclareLaunchArgument, context) -> None:
    """Resolve a DeclareLaunchArgument default and apply it to the launch context.

    Used in the Python-shim path (``_inline_resolve_python_launch``) to apply arg
    defaults before executing OpaqueFunction actions — matching official behaviour
    where DeclareLaunchArgument.execute() is called during the walk.
    """
    from roscope.entities.helpers import resolve_value

    if not arg.name:
        return

    if arg._condition is not None:
        try:
            if not arg._condition.evaluate(context):
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
        # Record the declared default for --show-args display (same as execute()).
        dv = arg.default_value
        display = resolve_value(dv, context) or "" if dv is not None else ""
        _record_and_track(arg.name, display, context)
        return

    # Apply default immediately — matching official DeclareLaunchArgument.execute()
    resolved = resolve_value(arg.default_value, context) or ""
    context._launch_configurations[arg.name] = resolved
    _record_and_track(arg.name, resolved, context)
