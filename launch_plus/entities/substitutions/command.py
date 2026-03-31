"""``$(command cmd [on_stderr])`` substitution — preserved as-is."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.entities.state import LaunchContext


def _needs_quoting(tokens: list[Substitution]) -> bool:
    """Return True if the argument contains spaces or substitutions that need quoting."""
    from launch_plus.entities.substitution import TextSubstitution

    for t in tokens:
        if not isinstance(t, TextSubstitution):
            return True
        if " " in t.text:
            return True
    return False


def _serialize_arg(tokens: list[Substitution]) -> str:
    """Serialize a single argument, quoting if it contains spaces or substitutions."""
    inner = "".join(t.serialize() for t in tokens)
    if _needs_quoting(tokens):
        return f"'{inner}'"
    return inner


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
        from launch_plus.resolver import resolve_substitutions_from_tokens

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

    def serialize(self) -> str:
        parts: list[str] = []
        for arg in self.arguments:
            inner = "".join(t.serialize() for t in arg)
            if _needs_quoting(arg):
                parts.append(f"'{inner}'")
            else:
                parts.append(inner)
        return f"$(command {' '.join(parts)})"
