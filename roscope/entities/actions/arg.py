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

        state = context._state

        if self._fixed_value is not None:
            # <arg name="x" value="v"/> — fixed value, set immediately
            resolved = resolve_value(self._fixed_value, context) or ""
            context._launch_configurations[name] = resolved
            _track_arg_default(name, resolved, state)
            return None

        # Resolve the declared default for --show-args display regardless of whether
        # the arg is already set in context. This matches old behavior: show the
        # declared default, not whatever value may have been set later by <let> etc.
        declared_default = ""
        if self.default_value is not None:
            declared_default = resolve_value(self.default_value, context) or ""

        # Matching official DeclareLaunchArgument.execute():
        # if already set (passed by parent include), leave unchanged.
        if name in context._launch_configurations:
            _track_arg_default(name, declared_default, state)
            return None

        # Not set — apply default immediately (matching official: no deferred resolution).
        if self.default_value is None:
            # No default and not set: at runtime this raises InvalidLaunchArgument.
            logger.error("arg '%s' is required but not set", name)
            _track_arg_default(name, "", state)
            return None

        context._launch_configurations[name] = declared_default
        _track_arg_default(name, declared_default, state)
        return None


def _track_arg_default(name: str, default: str, state) -> None:
    """Record a declared arg name and its resolved default in global and per-file dicts."""
    state.declared_arg_names.add(name)
    key = state.current_source_key()
    if key:
        # setdefault preserves the first-seen default value for repeated declarations.
        state.declared_arg_names_by_file.setdefault(key, {}).setdefault(name, default)


def _apply_declared_arg(arg: DeclareLaunchArgument, context) -> None:
    """Resolve a DeclareLaunchArgument default and apply it to the launch context.

    Matches official ``DeclareLaunchArgument.execute()`` behavior: if the arg is
    already set in context (e.g. from a parent include or command line), leave it
    unchanged; otherwise apply the declared default.

    Condition handling mirrors ``Action.visit()``: if the condition evaluates to
    False the declaration is skipped; if evaluation raises, a warning is emitted
    and the declaration is conservatively applied (unknown condition = assume active).
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

    declared_default = ""
    if arg.default_value is not None:
        declared_default = resolve_value(arg.default_value, context) or ""

    if arg.default_value is None:
        if arg.name not in context._launch_configurations:
            logger.error("arg '%s' is required but not set", arg.name)
        _track_arg_default(arg.name, "", context._state)
        return

    already_set = context is not None and arg.name in context._launch_configurations
    if already_set:
        _track_arg_default(arg.name, declared_default, context._state)
        return

    # Apply default immediately — matching official DeclareLaunchArgument.execute()
    context._launch_configurations[arg.name] = declared_default
    _track_arg_default(arg.name, declared_default, context._state)
