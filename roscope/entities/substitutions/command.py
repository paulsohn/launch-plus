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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/command.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""``$(command cmd [on_stderr])`` substitution — preserved as-is."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext

logger = logging.getLogger("roscope")


@expose_substitution("command")
class CommandSubstitution(Substitution):
    """Preserve ``$(command <cmd> [on_stderr])`` — cannot be evaluated at static analysis time.

    The command substitution takes 1-2 arguments:
    - ``cmd``: the full command string (may contain substitutions)
    - ``on_stderr``: optional stderr handling (e.g. ``warn``)
    """

    def __init__(self, *, arguments: list[list[Substitution]]) -> None:
        self.arguments = arguments

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[CommandSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(command ...) requires a command argument")
        arguments: list[list[Substitution]] = []
        for arg in args:
            if isinstance(arg, list):
                arguments.append(arg)
            else:
                arguments.append([arg])
        return cls, {"arguments": arguments}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import _current_file

        logger.warning(
            "%s: $(command ...) substitution is not executed by roscope; "
            "the literal expression will appear in the resolved output — "
            "topology depending on this value may be incorrect",
            _current_file(ctx),
        )
        from roscope.entities.helpers import resolve_substitutions_from_tokens

        resolved_args: list[str] = []
        for arg_tokens in self.arguments:
            resolved = resolve_substitutions_from_tokens(arg_tokens, ctx)
            resolved_args.append(resolved)

        # Quote arguments that contain spaces (to preserve argument boundaries).
        parts: list[str] = []
        for resolved in resolved_args:
            if " " in resolved:
                parts.append(f"'{resolved}'")
            else:
                parts.append(resolved)
        return f"$(command {' '.join(parts)})"

    def __str__(self) -> str:
        parts: list[str] = []
        for arg_tokens in self.arguments:
            inner = "".join(str(t) for t in arg_tokens)
            if " " in inner:
                parts.append(f"'{inner}'")
            else:
                parts.append(inner)
        return f"$(command {' '.join(parts)})"
