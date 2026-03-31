"""``$(find-pkg-share pkg)`` substitution — resolves a package share directory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.entities.state import LaunchContext, ResolverState

logger = logging.getLogger("launch_plus")


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

    def perform(self, ctx: LaunchContext) -> str:
        from launch_plus.entities.helpers import (
            resolve_substitutions_from_tokens,
        )

        state = ctx._state
        pkg = resolve_substitutions_from_tokens(self.package, ctx)
        state.track_package(pkg)
        if ctx.preview_mode:
            return f"$(find-pkg-share {pkg})"
        try:
            return state.resolve_pkg_share(pkg)
        except Exception:
            return f"$(find-pkg-share {pkg})"

    def serialize(self) -> str:
        return f"$(find-pkg-share {''.join(t.serialize() for t in self.package)})"


# ─── Python-shim substitution ────────────────────────────────────────────────


class FindPackageShare(Substitution):
    """Tracks FindPackageShare; package may be a string or a list of substitutions."""

    def __init__(self, package):
        self._package_subs = package

    def _resolve_name(self, context=None):
        """Resolve package name. Returns ``(name, is_fallback)``."""
        subs = self._package_subs
        if isinstance(subs, str):
            return subs, False
        if context is not None:
            result = context.perform_substitution(subs)
            if result is not None:
                return result, False
        return str(subs), True

    def _try_ament_resolve(self, state: ResolverState, pkg: str) -> str:
        """Resolve to a real path, return portable form on failure."""
        try:
            return state.resolve_pkg_share(pkg)
        except Exception as e:
            if not state.preview_mode:
                logger.error("$(find-pkg-share %s): %s", pkg, e)
            return f"$(find-pkg-share {pkg})"

    def perform(self, context, **kwargs):
        import launch_plus.resolver as _R

        state = context._state if context is not None else _R.get_state()
        pkg, is_fallback = self._resolve_name(context)
        if not is_fallback:
            state.track_package(pkg)
        if not state.preview_mode:
            return self._try_ament_resolve(state, pkg)
        return f"$(find-pkg-share {pkg})"

    def serialize(self) -> str:
        pkg, _ = self._resolve_name(None)
        return f"$(find-pkg-share {pkg})"

    def __str__(self):
        return self.serialize()
