# Copyright 2022 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/equals_substitution.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Equality substitutions: ``$(equals ...)``, ``$(not-equals ...)``."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext

_BOOL_STRINGS = frozenset(("true", "false", "1", "0"))
_BOOL_TRUE = frozenset(("true", "1"))


def _resolve_tokens(tokens: list, ctx: LaunchContext) -> str:
    from roscope.entities.helpers import resolve_substitutions_from_tokens

    return resolve_substitutions_from_tokens(tokens, ctx)


def _equals(left: str, right: str) -> bool:
    """Compare two resolved strings with type coercion, matching official ROS 2."""
    # Boolean comparison
    if left.lower() in _BOOL_STRINGS and right.lower() in _BOOL_STRINGS:
        return (left.lower() in _BOOL_TRUE) == (right.lower() in _BOOL_TRUE)
    # Float comparison (epsilon-close)
    try:
        lf, rf = float(left), float(right)
        return math.isclose(lf, rf)
    except (ValueError, TypeError):
        pass
    # String equality
    return left == right


@expose_substitution("equals")
class EqualsSubstitution(Substitution):
    """``$(equals left right)`` → ``"true"`` or ``"false"``."""

    def __init__(self, *, left: list[Substitution], right: list[Substitution]) -> None:
        self._left = left
        self._right = right

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[EqualsSubstitution], dict[str, Any]]:
        if len(args) != 2:
            raise TypeError("$(equals ...) expects 2 arguments")
        left = args[0] if isinstance(args[0], list) else [args[0]]
        right = args[1] if isinstance(args[1], list) else [args[1]]
        return cls, {"left": left, "right": right}

    def perform(self, ctx: LaunchContext) -> str:
        left = _resolve_tokens(self._left, ctx)
        right = _resolve_tokens(self._right, ctx)
        return str(_equals(left, right)).lower()

    def __str__(self) -> str:
        left_s = "".join(str(t) for t in self._left)
        right_s = "".join(str(t) for t in self._right)
        return f"$(equals {left_s} {right_s})"


@expose_substitution("not-equals")
class NotEqualsSubstitution(Substitution):
    """``$(not-equals left right)`` → ``"true"`` or ``"false"``."""

    def __init__(self, *, left: list[Substitution], right: list[Substitution]) -> None:
        self._left = left
        self._right = right

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[NotEqualsSubstitution], dict[str, Any]]:
        if len(args) != 2:
            raise TypeError("$(not-equals ...) expects 2 arguments")
        left = args[0] if isinstance(args[0], list) else [args[0]]
        right = args[1] if isinstance(args[1], list) else [args[1]]
        return cls, {"left": left, "right": right}

    def perform(self, ctx: LaunchContext) -> str:
        left = _resolve_tokens(self._left, ctx)
        right = _resolve_tokens(self._right, ctx)
        return str(not _equals(left, right)).lower()

    def __str__(self) -> str:
        left_s = "".join(str(t) for t in self._left)
        right_s = "".join(str(t) for t in self._right)
        return f"$(not-equals {left_s} {right_s})"
