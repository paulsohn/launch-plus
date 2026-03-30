"""``$(eval expr)`` substitution — evaluates a Python expression."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.resolver import _SubstitutionContext


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
        from launch_plus.entities.substitution import TextSubstitution

        parts: list[Substitution] = []
        for i, arg in enumerate(args):
            if i > 0:
                parts.append(TextSubstitution(text=" "))
            if isinstance(arg, list):
                parts.extend(arg)
            else:
                parts.append(arg)
        return cls, {"expression": parts}

    def perform(self, ctx: _SubstitutionContext, *, _depth: int = 0) -> str:
        from launch_plus.resolver import resolve_substitutions_from_tokens

        expr = resolve_substitutions_from_tokens(self.expression, ctx, _depth=_depth + 1)
        # Unescape \' and \" that may come from XML entity values.
        expr = expr.replace("\\'", "'").replace('\\"', '"')
        try:
            result = eval(expr)  # noqa: S307
            return str(result)
        except Exception as e:
            ctx._state.error(f"$(eval {expr}) failed: {e}")
            if ctx.preview_mode:
                return f"$(eval {expr})"
            return ""

    def serialize(self) -> str:
        return f"$(eval {''.join(t.serialize() for t in self.expression)})"
