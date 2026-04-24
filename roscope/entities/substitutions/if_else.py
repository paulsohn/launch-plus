# Copyright 2023 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/if_else_substitution.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Conditional substitution: ``$(if condition if_value [else_value])``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution
from roscope.entities.substitutions.boolean import _coerce_bool

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext


def _resolve_tokens(tokens: list, ctx: LaunchContext) -> str:
    from roscope.entities.helpers import resolve_substitutions_from_tokens

    return resolve_substitutions_from_tokens(tokens, ctx)


@expose_substitution("if")
class IfElseSubstitution(Substitution):
    """``$(if condition if_value [else_value])``."""

    def __init__(
        self,
        *,
        condition: list[Substitution],
        if_value: list[Substitution],
        else_value: list[Substitution] | None = None,
    ) -> None:
        self._condition = condition
        self._if_value = if_value
        self._else_value = else_value

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[IfElseSubstitution], dict[str, Any]]:
        if len(args) < 2 or len(args) > 3:
            raise TypeError("$(if ...) expects 2 or 3 arguments")
        condition = args[0] if isinstance(args[0], list) else [args[0]]
        if_value = args[1] if isinstance(args[1], list) else [args[1]]
        else_value = None
        if len(args) == 3:
            else_value = args[2] if isinstance(args[2], list) else [args[2]]
        return cls, {"condition": condition, "if_value": if_value, "else_value": else_value}

    def perform(self, ctx: LaunchContext) -> str:
        cond = _coerce_bool(_resolve_tokens(self._condition, ctx))
        if cond:
            return _resolve_tokens(self._if_value, ctx)
        if self._else_value is not None:
            return _resolve_tokens(self._else_value, ctx)
        return ""

    def __str__(self) -> str:
        c = "".join(str(t) for t in self._condition)
        iv = "".join(str(t) for t in self._if_value)
        if self._else_value is not None:
            ev = "".join(str(t) for t in self._else_value)
            return f"$(if {c} {iv} {ev})"
        return f"$(if {c} {iv})"
