"""``$(find-pkg-prefix pkg)`` substitution — kept in portable form."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.entities.state import LaunchContext


@expose_substitution("find-pkg-prefix")
class FindPackagePrefixSubstitution(Substitution):
    """Resolve ``$(find-pkg-prefix <pkg>)`` — always kept in portable form."""

    def __init__(self, *, package: list[Substitution]) -> None:
        self.package = package

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[FindPackagePrefixSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(find-pkg-prefix ...) requires a package argument")
        return cls, {"package": args[0] if isinstance(args[0], list) else [args[0]]}

    def perform(self, ctx: LaunchContext) -> str:
        from launch_plus.entities.helpers import (
            resolve_substitutions_from_tokens,
        )

        state = ctx._state
        pkg = resolve_substitutions_from_tokens(self.package, ctx)
        state.track_package(pkg)
        # Always keep portable — prefix resolution not implemented.
        return f"$(find-pkg-prefix {pkg})"

    def serialize(self) -> str:
        return f"$(find-pkg-prefix {''.join(t.serialize() for t in self.package)})"
