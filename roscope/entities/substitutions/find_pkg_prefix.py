"""``$(find-pkg-prefix pkg)`` substitution — resolves a package prefix directory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext


@expose_substitution("find-pkg-prefix")
class FindPackagePrefixSubstitution(Substitution):
    """Resolve ``$(find-pkg-prefix <pkg>)`` to the package's install prefix."""

    def __init__(self, *, package: list[Substitution]) -> None:
        self.package = package

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[FindPackagePrefixSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(find-pkg-prefix ...) requires a package argument")
        return cls, {"package": args[0] if isinstance(args[0], list) else [args[0]]}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import resolve_substitutions_from_tokens

        state = ctx._state
        pkg = resolve_substitutions_from_tokens(self.package, ctx)
        state.track_package(pkg)
        share = state.resolve_pkg_share(pkg)
        # Prefix is the parent of share/<pkg> — e.g. /opt/ros/humble
        return str(Path(share).parent.parent)

    def __str__(self) -> str:
        return f"$(find-pkg-prefix {''.join(str(t) for t in self.package)})"
