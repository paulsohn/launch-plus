"""``$(find-pkg-share pkg)`` substitution — resolves a package share directory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.resolver import _SubstitutionContext


@expose_substitution("find-pkg-share")
class FindPackageShareSubstitution(Substitution):
    """Resolve ``$(find-pkg-share <pkg>)`` to the package's share directory.

    In preview mode, returns the portable form ``$(find-pkg-share <pkg>)``.
    In resolved mode, returns the actual filesystem path.
    """

    def __init__(self, *, package: list[Substitution]) -> None:
        self.package = package

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[FindPackageShareSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(find-pkg-share ...) requires a package argument")
        return cls, {"package": args[0] if isinstance(args[0], list) else [args[0]]}

    def perform(self, ctx: _SubstitutionContext, *, _depth: int = 0) -> str:
        from launch_plus.resolver import (
            _PackageNotFetchedError,
            _resolve_pkg_share,
            _track_package,
            resolve_substitutions_from_tokens,
        )

        pkg = resolve_substitutions_from_tokens(self.package, ctx, _depth=_depth + 1)
        _track_package(pkg)
        if ctx.preview_mode:
            return f"$(find-pkg-share {pkg})"
        try:
            return _resolve_pkg_share(pkg)
        except _PackageNotFetchedError:
            raise
        except Exception:
            return f"$(find-pkg-share {pkg})"

    def serialize(self) -> str:
        return f"$(find-pkg-share {''.join(t.serialize() for t in self.package)})"
