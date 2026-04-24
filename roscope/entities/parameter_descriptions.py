# Copyright 2020 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Originally from:
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/parameter_descriptions.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Parameter descriptions matching official launch_ros/parameter_descriptions.py.

Provides ``Parameter`` and ``ParameterFile`` types that both XML and Python
shim paths produce. ``evaluate(context)`` resolves substitutions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from roscope.entities.utilities import normalize_to_list_of_substitutions, perform_substitutions

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext
    from roscope.entities.substitution import Substitution

logger = logging.getLogger("roscope")


# ─── Parameter YAML expansion ────────────────────────────────────────────────


def _expand_ros_params_yaml(
    content: str,
    context: LaunchContext | None = None,
) -> list[tuple[str, str]]:
    """Parse ROS 2 parameter YAML and flatten into (key, value) pairs.

    If *context* is given, ROS 2 substitutions inside each scalar value are
    resolved after YAML parsing (value-level, not text-level, so the YAML
    structure is never broken by substituted path strings containing ``[`` or
    ``{``).
    """
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    _collect_ros_params(data, 0, out)
    if context is None:
        return out
    from roscope.entities.helpers import resolve_substitutions

    result: list[tuple[str, str]] = []
    for key, val in out:
        try:
            resolved_val = resolve_substitutions(val, context)
        except Exception:
            logger.exception("failed to resolve substitutions in param value %r: %r", key, val)
            resolved_val = val
        result.append((key, resolved_val))
    return result


def _collect_ros_params(value: object, depth: int, out: list[tuple[str, str]]) -> None:
    if depth > 3 or not isinstance(value, dict):
        return
    if "ros__parameters" in value:
        _flatten_yaml_value(value["ros__parameters"], "", out)
    else:
        for child in value.values():
            if isinstance(child, dict):
                _collect_ros_params(child, depth + 1, out)


def _flatten_yaml_value(value: object, prefix: str, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            full_key = f"{prefix}.{k}" if prefix else str(k)
            _flatten_yaml_value(v, full_key, out)
    elif isinstance(value, list):
        items = ", ".join(_yaml_value_to_str(v) for v in value)
        out.append((prefix, f"[{items}]"))
    else:
        out.append((prefix, _yaml_value_to_str(value)))


def _yaml_value_to_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, list):
        items = ", ".join(_yaml_value_to_str(x) for x in v)
        return f"[{items}]"
    return str(v)


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
        self.__allow_substs: bool = bool(allow_substs)

    @property
    def param_file(self) -> list[Substitution] | str:
        return self.__param_file

    @property
    def allow_substs(self) -> bool:
        return self.__allow_substs

    def evaluate(self, context: LaunchContext) -> tuple[Path, list[tuple[str, str]]]:
        """Evaluate and return (path, params).

        Always reads and inlines the file.  If ``allow_substs`` is True,
        ROS 2 substitutions inside each YAML scalar value are resolved after
        parsing (value-level, not text-level).
        Raises ``FileNotFoundError`` if the file does not exist.

        TODO: cache expanded results keyed by resolved path for the duration
        of a resolve run to avoid redundant disk I/O when the same file is
        referenced by multiple nodes.
        """
        param_file = self.__param_file
        if isinstance(param_file, list):
            param_file = perform_substitutions(context, param_file)

        param_file_path: Path = Path(param_file)
        if not param_file_path.is_file():
            raise FileNotFoundError(f"param file not found: '{param_file_path}'")

        with open(param_file_path) as f:
            content = f.read()

        try:
            return param_file_path, _expand_ros_params_yaml(
                content, context if self.__allow_substs else None
            )
        except yaml.YAMLError as e:
            raise yaml.YAMLError(f"Error parsing param file '{param_file_path}': {e}") from e
