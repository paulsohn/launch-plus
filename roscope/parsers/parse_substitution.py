# Copyright 2019 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/frontend/parse_substitution.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Lark-based substitution parser.

Parses strings containing ``$(...)`` substitution expressions into lists of
:class:`~roscope.entities.substitution.Substitution` objects.

Adapted from ROS 2 ``launch.frontend.parse_substitution``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lark import Lark, Token, Transformer

from roscope.entities.expose import instantiate_substitution
from roscope.entities.substitution import Substitution, TextSubstitution


def replace_escaped_characters(data: str) -> str:
    """Replace backslash-escaped characters with the character itself."""
    return re.sub(r"\\(.)", r"\1", data)


class ExtractSubstitution(Transformer):  # type: ignore[type-arg]
    """Lark transformer that converts a parse tree into Substitution objects."""

    # ── part (inside substitution arguments) ──────────────────────────────

    def part(self, content: list[Token | Substitution]) -> Substitution:
        assert len(content) == 1
        item = content[0]
        if isinstance(item, Token):
            assert item.type.endswith("_RSTRING")
            return TextSubstitution(text=replace_escaped_characters(item.value))
        return item  # type: ignore[return-value]

    single_quoted_part = part
    double_quoted_part = part

    # ── value (one argument to a substitution) ────────────────────────────

    def value(self, parts: list[Substitution | list[Substitution]]) -> list[Substitution]:
        if len(parts) == 1 and isinstance(parts[0], list):
            # Quoted template — already a list of substitutions.
            return parts[0]
        return parts  # type: ignore[return-value]

    single_quoted_value = value
    double_quoted_value = value

    # ── arguments (space-separated values, tail-recursive) ────────────────

    def arguments(self, values: list[Any]) -> list[Any]:
        if len(values) > 1:
            # Tail-recursive: first element is the accumulated list.
            return [*values[0], values[1]]
        return values

    single_quoted_arguments = arguments
    double_quoted_arguments = arguments

    # ── substitution — $(IDENTIFIER arguments?) ───────────────────────────

    def substitution(self, args: list[Any]) -> Substitution:
        assert len(args) >= 1
        name = args[0]
        assert isinstance(name, Token)
        assert name.type == "IDENTIFIER"
        # args[1] (if present) is the ``arguments`` result: a list of values,
        # where each value is itself a list of Substitution objects.
        subst_args = args[1] if len(args) > 1 else []
        return instantiate_substitution(name.value, subst_args)

    single_quoted_substitution = substitution
    double_quoted_substitution = substitution

    # ── fragment (top-level piece of a template) ──────────────────────────

    def fragment(self, content: list[Token | Substitution]) -> Substitution:
        assert len(content) == 1
        item = content[0]
        if isinstance(item, Token):
            assert item.type.endswith("_STRING")
            return TextSubstitution(text=replace_escaped_characters(item.value))
        return item  # type: ignore[return-value]

    single_quoted_fragment = fragment
    double_quoted_fragment = fragment

    # ── template (the full parsed expression) ─────────────────────────────

    def template(self, fragments: list[Substitution]) -> list[Substitution]:
        return fragments

    single_quoted_template = template
    double_quoted_template = template


# ── Module-level cached parser ───────────────────────────────────────────────

_grammar_path = Path(__file__).parent / "grammar.lark"
_parser: Lark | None = None


def parse_substitution(string_value: str) -> list[Substitution]:
    """Parse *string_value* into a list of :class:`Substitution` objects.

    Returns ``[TextSubstitution(text=string_value)]`` for empty or
    substitution-free strings.
    """
    global _parser  # noqa: PLW0603

    if not string_value:
        return [TextSubstitution(text=string_value)]

    if _parser is None:
        # TODO: official also omits parser=; adding parser="lalr" would be faster
        # but requires verifying the grammar is LALR(1)-compatible.
        _parser = Lark(_grammar_path.read_text(), start="template")

    tree = _parser.parse(string_value)
    transformer = ExtractSubstitution()
    return transformer.transform(tree)  # type: ignore[no-any-return]
