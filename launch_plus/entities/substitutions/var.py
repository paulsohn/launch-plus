"""``$(var name)`` substitution — resolves a variable (``<let>``) or argument."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.resolver import _SubstitutionContext


@expose_substitution("var")
class VarSubstitution(Substitution):
    """Resolve ``$(var <name>)`` — checks ``ctx.vars`` first, then ``ctx.args``."""

    def __init__(self, *, name: list[Substitution]) -> None:
        self.name = name

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[VarSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(var ...) requires a name argument")
        return cls, {"name": args[0] if isinstance(args[0], list) else [args[0]]}

    def perform(self, ctx: _SubstitutionContext, *, _depth: int = 0) -> str:
        from launch_plus.entities.substitutions.arg import parse_to_tokens
        from launch_plus.resolver import resolve_substitutions_from_tokens

        name = resolve_substitutions_from_tokens(self.name, ctx, _depth=_depth + 1)
        if name in ctx.vars:
            value = ctx.vars[name]
        elif name in ctx.args:
            value = ctx.args[name]
        else:
            value = None
        if value is None:
            ctx._state.error(f"undefined variable: {name}")
            return f"$(var {name})"
        return resolve_substitutions_from_tokens(parse_to_tokens(value), ctx, _depth=_depth + 1)

    def serialize(self) -> str:
        return f"$(var {''.join(t.serialize() for t in self.name)})"
