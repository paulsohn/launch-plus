"""Parameter-related action handlers and tracked Python-shim actions.

Covers: <let> (XML), SetLaunchConfiguration / SetParameter / ParameterFile (Python shim).
"""

from __future__ import annotations

from launch_plus.entities.action import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity


class _TrackedParameterFile(_TrackedAction):
    """Tracks ParameterFile references so they can be reported as param_file dependencies."""

    def __init__(self, param_file=None, *args, allow_substs=False, **kwargs):
        if param_file is None and args:
            param_file = args[0]
        # Store raw — resolve lazily (matches official: no eager resolution)
        self._param_file = param_file if isinstance(param_file, str) else None
        self._raw_param_file = param_file


@expose_action("let")
class _SetLaunchConfiguration(_TrackedAction):
    """Implements SetLaunchConfiguration / <let>: updates launch_configurations."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        name = entity.get_attr("name", optional=True) or ""
        value = parser.parse_substitution(entity.get_attr("value", optional=True) or "")
        return cls(name=name, value=value)

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value

    def execute(self, context) -> list | None:
        from launch_plus.entities.helpers import resolve_value

        name = str(self._name) if self._name else ""
        if not name:
            return None
        value = resolve_value(self._value, context)
        resolved_value = str(value) if value is not None else ""
        # Set _launch_configurations (single source of truth)
        if context is not None and hasattr(context, "_launch_configurations"):
            context._launch_configurations[name] = resolved_value
        context._state.tracked["set_launch_configurations"][name] = resolved_value
        return None


@expose_action("set_parameter")
class _TrackedSetParameter(_TrackedAction):
    """Mirrors launch_ros SetParameter / <set_parameter>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        name = parser.parse_substitution(entity.get_attr("name", optional=True) or "")
        value = parser.parse_substitution(entity.get_attr("value", optional=True) or "")
        return cls(name=name, value=value)

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value

    def execute(self, context) -> list | None:
        from launch_plus.entities.helpers import resolve_value

        name = resolve_value(self._name, context)
        if not name:
            return None
        value: object = resolve_value(self._value, context)
        if isinstance(value, str):
            try:
                value = int(value)
            except (ValueError, TypeError):
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    pass
        # Matching official SetParameter: write to launch_configurations['global_params']
        gp_list = context._launch_configurations.setdefault("global_params", [])
        gp_list.append((name, value))
        context._state.tracked["global_params"].append([name, value])
        return None
