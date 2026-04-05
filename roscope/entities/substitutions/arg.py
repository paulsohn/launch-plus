"""``$(arg name)`` substitution — resolves a launch argument."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

logger = logging.getLogger("roscope")

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext


@expose_substitution("arg")
class ArgSubstitution(Substitution):
    """Resolve ``$(arg <name>)`` to the value of launch argument *name*."""

    def __init__(self, *, name: list[Substitution]) -> None:
        self.name = name

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[ArgSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(arg ...) requires a name argument")
        return cls, {"name": args[0] if isinstance(args[0], list) else [args[0]]}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import resolve_substitutions_from_tokens
        from roscope.entities.substitutions.launch_config import DeferredDefault

        name = resolve_substitutions_from_tokens(self.name, ctx)
        lc = getattr(ctx, "_launch_configurations", {})
        value = lc.get(name)
        if value is None:
            logger.error("undefined argument: %s", name)
            return f"$(arg {name})"
        if isinstance(value, DeferredDefault):
            resolved = value.resolve(ctx)
            lc[name] = resolved
            value = resolved
        return resolve_substitutions_from_tokens(parse_to_tokens(value), ctx)

    def __str__(self) -> str:
        return f"$(arg {''.join(str(t) for t in self.name)})"


def parse_to_tokens(text: str) -> list[Substitution]:
    """Parse a string into substitution tokens (lazy import to avoid cycles)."""
    from roscope.parsers.parse_substitution import parse_substitution

    return parse_substitution(text)
