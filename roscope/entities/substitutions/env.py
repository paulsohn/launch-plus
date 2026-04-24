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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/environment_variable.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""``$(env NAME [default])`` substitution — resolves an environment variable.

Mirrors ``launch.substitutions.EnvironmentVariable``:

- Accepts a name that is a ``str``, a single :class:`Substitution`, or a
  ``list[Substitution]``, matching the Python launch API.
- ``parse()`` classmethod handles the XML ``$(env NAME [default])`` syntax.
- When the variable is unset and no default is given, logs an error and
  returns an empty string (rather than raising), so resolution continues
  and the caller can see all missing variables in one pass.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution, TextSubstitution

logger = logging.getLogger("roscope")

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext

_SomeSubstitutionsType = str | Substitution | list[Substitution | str]


def _normalize(value: _SomeSubstitutionsType) -> list[Substitution]:
    """Normalize a name/default value to a list of Substitution objects."""
    if isinstance(value, list):
        return [TextSubstitution(text=t) if isinstance(t, str) else t for t in value]
    if isinstance(value, str):
        return [TextSubstitution(text=value)]
    return [value]


@expose_substitution("env")
class EnvironmentVariable(Substitution):
    """Resolve an environment variable by name.

    Handles both the XML ``$(env NAME [default])`` form (via :meth:`parse`)
    and the Python ``EnvironmentVariable(name, default_value=...)`` API.
    """

    def __init__(
        self,
        name: _SomeSubstitutionsType,
        *,
        default_value: _SomeSubstitutionsType | None = None,
    ) -> None:
        self._name: list[Substitution] = _normalize(name)
        self._default: list[Substitution] | None = (
            _normalize(default_value) if default_value is not None else None
        )

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[EnvironmentVariable], dict[str, Any]]:
        """Parse ``$(env NAME [default])``."""
        if not args:
            raise ValueError("$(env ...) requires at least a name argument")
        if len(args) > 2:
            raise ValueError("$(env ...) accepts only NAME and an optional default value")
        name = args[0] if isinstance(args[0], list) else [args[0]]
        kwargs: dict[str, Any] = {"name": name}
        if len(args) > 1:
            kwargs["default_value"] = args[1] if isinstance(args[1], list) else [args[1]]
        return cls, kwargs

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import resolve_substitutions_from_tokens

        name = resolve_substitutions_from_tokens(self._name, ctx)
        value = ctx.environment.get(name) if hasattr(ctx, "environment") else None
        if value is None:
            value = os.environ.get(name)
        if value is None and self._default is not None:
            value = resolve_substitutions_from_tokens(self._default, ctx)
        if value is None:
            from roscope.entities.helpers import _current_file

            logger.error(
                "%s: environment variable '%s' is not set; using empty string",
                _current_file(ctx),
                name,
            )
            return ""
        return str(value)

    def __str__(self) -> str:
        name_str = "".join(str(t) for t in self._name)
        if self._default is not None:
            default_str = "".join(str(t) for t in self._default)
            return f"$(env {name_str} {default_str})"
        return f"$(env {name_str})"
