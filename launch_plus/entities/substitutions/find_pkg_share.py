"""``$(find-pkg-share pkg)`` substitution — resolves a package share directory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

logger = logging.getLogger("launch_plus")

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

    def perform(self, ctx: _SubstitutionContext) -> str:
        from launch_plus.resolver import (
            _resolve_pkg_share,
            _track_package,
            resolve_substitutions_from_tokens,
        )

        state = ctx._state
        pkg = resolve_substitutions_from_tokens(self.package, ctx)
        _track_package(state, pkg)
        if ctx.preview_mode:
            return f"$(find-pkg-share {pkg})"
        try:
            return _resolve_pkg_share(state, pkg)
        except Exception:
            return f"$(find-pkg-share {pkg})"

    def serialize(self) -> str:
        return f"$(find-pkg-share {''.join(t.serialize() for t in self.package)})"


# ─── Python-shim substitution ────────────────────────────────────────────────


class _TrackedFindPackageShare:
    """Tracks FindPackageShare; package may be a string or a list of substitutions."""

    def __init__(self, package):
        self._package_subs = package

    def _resolve_name(self, context=None):
        """Concatenate package name from string or list of substitution objects.

        Returns ``(name, is_fallback)`` where *is_fallback* is ``True`` when
        any part could not be resolved and the display name was used instead.
        """
        subs = self._package_subs
        if isinstance(subs, str):
            return subs, False
        if isinstance(subs, list):
            parts = []
            any_fallback = False
            for sub in subs:
                if context is not None and hasattr(sub, "perform"):
                    result = sub.perform(context)
                    if result is not None:
                        parts.append(str(result))
                    else:
                        parts.append(str(sub))
                        any_fallback = True
                else:
                    parts.append(str(sub))
                    if hasattr(sub, "perform"):
                        any_fallback = True
            return "".join(parts), any_fallback
        if hasattr(subs, "perform"):
            return str(subs), True
        return str(subs), False

    def _try_ament_resolve(self, state, pkg: str) -> str:
        """Resolve to a real path, return portable form on failure."""
        import launch_plus.resolver as _R

        try:
            return _R._resolve_pkg_share(state, pkg)
        except Exception as e:
            if not state.preview_mode:
                logger.error("$(find-pkg-share %s): %s", pkg, e)
            return f"$(find-pkg-share {pkg})"

    def perform(self, context, **kwargs):
        import launch_plus.resolver as _R
        from launch_plus.entities.helpers import _track_package

        state = context._state if context is not None else _R.get_state()
        pkg, is_fallback = self._resolve_name(context)
        if not is_fallback:
            _track_package(state, pkg)
        if not state.preview_mode:
            return self._try_ament_resolve(state, pkg)
        return f"$(find-pkg-share {pkg})"

    def __str__(self):
        import launch_plus.resolver as _R

        state = _R.get_state()
        pkg, _ = self._resolve_name(None)
        if not state.preview_mode:
            return self._try_ament_resolve(state, pkg)
        return f"$(find-pkg-share {pkg})"
