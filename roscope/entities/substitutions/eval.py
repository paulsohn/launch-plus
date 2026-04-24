# Copyright 2018 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/python_expression.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""``$(eval expr)`` substitution — evaluates a Python expression."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

logger = logging.getLogger("roscope")

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext


@expose_substitution("eval")
class EvalSubstitution(Substitution):
    """Resolve ``$(eval <expr>)`` by evaluating *expr* as a Python expression."""

    def __init__(self, *, expression: list[Substitution]) -> None:
        self.expression = expression

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[EvalSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(eval ...) requires an expression argument")
        # $(eval 1 + 2) arrives as three arguments: ["1", "+", "2"].
        # Join them all with spaces to reconstruct the full expression,
        # matching the behavior of the old hand-rolled parser.
        from roscope.entities.substitution import TextSubstitution

        parts: list[Substitution] = []
        for i, arg in enumerate(args):
            if i > 0:
                parts.append(TextSubstitution(text=" "))
            if isinstance(arg, list):
                parts.extend(arg)
            else:
                parts.append(arg)
        return cls, {"expression": parts}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import resolve_substitutions_from_tokens

        expr = resolve_substitutions_from_tokens(self.expression, ctx)
        # Unescape \' and \" that may come from XML entity values.
        expr = expr.replace("\\'", "'").replace('\\"', '"')
        try:
            result = eval(expr)  # noqa: S307
            return str(result)
        except Exception as e:
            from roscope.entities.helpers import _current_file

            logger.error("%s: $(eval %s) failed: %s", _current_file(ctx), expr, e)
            if ctx.preview_mode:
                return f"$(eval {expr})"
            return ""

    def __str__(self) -> str:
        return f"$(eval {''.join(str(t) for t in self.expression)})"
