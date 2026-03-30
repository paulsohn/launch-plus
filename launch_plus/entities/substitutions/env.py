"""``$(env NAME [default])`` substitution — resolves an environment variable."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

logger = logging.getLogger("launch_plus")

if TYPE_CHECKING:
    from launch_plus.resolver import _SubstitutionContext


@expose_substitution("env")
class EnvSubstitution(Substitution):
    """Resolve ``$(env NAME [default])``."""

    def __init__(
        self,
        *,
        name: list[Substitution],
        default: list[Substitution] | None = None,
    ) -> None:
        self.name = name
        self.default = default

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[EnvSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(env ...) requires at least a name argument")
        name = args[0] if isinstance(args[0], list) else [args[0]]
        default = None
        if len(args) > 1:
            default = args[1] if isinstance(args[1], list) else [args[1]]
        return cls, {"name": name, "default": default}

    def perform(self, ctx: _SubstitutionContext, *, _depth: int = 0) -> str:
        from launch_plus.resolver import resolve_substitutions_from_tokens

        name = resolve_substitutions_from_tokens(self.name, ctx, _depth=_depth + 1)
        # Check overrides → process env → default
        value = ctx.env.get(name)
        if value is None:
            value = os.environ.get(name)
        if value is None and self.default is not None:
            value = resolve_substitutions_from_tokens(self.default, ctx, _depth=_depth + 1)
        if value is None:
            logger.error("environment variable not set: %s", name)
            return f"$(env {name})"
        return str(value)

    def serialize(self) -> str:
        name_str = "".join(t.serialize() for t in self.name)
        if self.default is not None:
            default_str = "".join(t.serialize() for t in self.default)
            return f"$(env {name_str} {default_str})"
        return f"$(env {name_str})"
