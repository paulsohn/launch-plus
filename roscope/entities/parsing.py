# Copyright 2019 Open Source Robotics Foundation, Inc.
# Copyright 2020 Open Avatar Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/frontend/parser.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Action parser facade for XML/YAML action handlers.

Provides :class:`Parser`, a stateless parsing helper that action
``parse()`` classmethods use to build unresolved action instances.
"""

from __future__ import annotations

from roscope.entities.helpers import _evaluate_condition, _is_truthy
from roscope.parsers.entity import Entity


class _ParsedCondition:
    """A parsed ``if=``/``unless=`` condition stored on an action.

    Mirrors official ``IfCondition``/``UnlessCondition``: stores substitution
    tokens and evaluates them at visit time against the current context.
    ``evaluate(context)`` returns True when the action should execute.
    """

    __slots__ = ("kind", "tokens")

    def __init__(self, kind: str, tokens: list) -> None:
        self.kind = kind  # "If" or "Unless"
        self.tokens = tokens

    def evaluate(self, context) -> bool:
        """Resolve tokens and return True if the action should execute."""
        from roscope.entities.helpers import resolve_substitutions_from_tokens

        try:
            resolved = resolve_substitutions_from_tokens(self.tokens, context)
            truthy = _is_truthy(resolved)
        except Exception:
            return False
        return truthy if self.kind == "If" else not truthy

    def __repr__(self) -> str:
        return f"_ParsedCondition({self.kind!r}, {self.tokens!r})"


class Parser:
    """Stateless parsing helper for action ``parse()`` classmethods.

    Provides substitution token parsing and condition evaluation.
    Does NOT resolve substitutions or access mutable state — ``parse()``
    methods use this to build unresolved action instances.
    """

    __slots__ = ("ctx", "include_stack")

    def __init__(self, ctx, include_stack: list[str]) -> None:
        self.ctx = ctx
        self.include_stack = include_stack

    def parse_substitution(self, text: str) -> list:
        """Parse ``$(...)`` substitutions in *text* into token objects.

        Returns a list of :class:`Substitution` objects that can be resolved
        later via ``resolve_substitutions_from_tokens()``.
        """
        from roscope.parsers.parse_substitution import parse_substitution as _lark_parse

        return _lark_parse(text)

    def parse_condition(self, entity: Entity) -> _ParsedCondition | None:
        """Parse if=/unless= on *entity* into a condition object.

        Returns a :class:`_ParsedCondition` that can be stored on an action
        and evaluated at visit time, or ``None`` if no condition is present.
        Matches official ``Action.parse()`` → ``IfCondition``/``UnlessCondition``.
        """
        if_val = entity.get_attr("if", optional=True)
        unless_val = entity.get_attr("unless", optional=True)
        if if_val is not None and unless_val is not None:
            raise RuntimeError("if= and unless= cannot be used simultaneously")
        if if_val is not None:
            return _ParsedCondition("If", self.parse_substitution(if_val))
        if unless_val is not None:
            return _ParsedCondition("Unless", self.parse_substitution(unless_val))
        return None

    def evaluate_condition(self, entity: Entity) -> bool:
        """Eagerly evaluate if=/unless= on *entity* at parse time.

        Used for inline sub-entity filtering (e.g. ``<composable_node>``
        children) where the element is not an ``Action`` and has no visit().
        For top-level actions, prefer ``parse_condition()`` + ``visit()``.
        """
        if_val = entity.get_attr("if", optional=True)
        unless_val = entity.get_attr("unless", optional=True)
        cond: dict[str, str] | None = None
        if if_val is not None:
            cond = {"kind": "If", "expr": if_val}
        elif unless_val is not None:
            cond = {"kind": "Unless", "expr": unless_val}
        return _evaluate_condition(cond, self.ctx)
