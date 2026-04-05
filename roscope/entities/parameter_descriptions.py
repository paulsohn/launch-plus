"""Parameter descriptions matching official launch_ros/parameter_descriptions.py.

Provides ``Parameter`` and ``ParameterFile`` types that both XML and Python
shim paths produce. ``evaluate(context)`` resolves substitutions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from roscope.entities.utilities import normalize_to_list_of_substitutions, perform_substitutions

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext
    from roscope.entities.substitution import Substitution


class Parameter:
    """Describes a ROS parameter (name + value pair).

    Matching official ``launch_ros.parameter_descriptions.Parameter``.
    """

    def __init__(self, name, value=None, **kwargs):
        if isinstance(name, str):
            self.__name = normalize_to_list_of_substitutions(name)
        else:
            self.__name = name  # already list[Substitution] from parser
        self.__value = value  # str, Substitution, or list[Substitution]

    @property
    def name(self):
        return self.__name

    @property
    def value(self) -> Any:
        return self.__value

    def evaluate(self, context: LaunchContext) -> tuple[str, str]:
        """Resolve name and value, return (name_str, value_str)."""
        name = (
            perform_substitutions(context, self.__name)
            if isinstance(self.__name, list)
            else str(self.__name)
        )
        value = context.perform_substitution(self.__value) if self.__value is not None else ""
        return (name, str(value))


class ParameterFile:
    """Describes a ROS parameter file path.

    Matching official ``launch_ros.parameter_descriptions.ParameterFile``.
    """

    def __init__(self, param_file=None, *args, allow_substs=False, **kwargs):
        if param_file is None and args:
            param_file = args[0]
        if isinstance(param_file, str):
            self.__param_file: list[Substitution] | str = param_file
        elif isinstance(param_file, list):
            self.__param_file = param_file  # list[Substitution] from parser
        elif param_file is not None:
            # Single substitution object — wrap in list
            self.__param_file = [param_file] if not isinstance(param_file, list) else param_file
        else:
            self.__param_file = ""

    @property
    def param_file(self) -> list[Substitution] | str:
        return self.__param_file

    def evaluate(self, context: LaunchContext) -> str:
        """Resolve the parameter file path."""
        if isinstance(self.__param_file, str):
            return self.__param_file
        return perform_substitutions(context, self.__param_file)
