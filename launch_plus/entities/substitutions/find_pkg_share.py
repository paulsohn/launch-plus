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


# ─── Python-shim substitution ────────────────────────────────────────────────


class _TrackedFindPackageShare:
    """Tracks FindPackageShare; package may be a string or a list of substitutions."""

    def __init__(self, package):
        import launch_plus.resolver as _R

        self._package_subs = package
        # Track statically when the name is a plain string
        if isinstance(package, str):
            _R._track_package(package)

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

    def _try_ament_resolve(self, pkg: str) -> str:
        """Resolve to a real path, return portable form on failure."""
        import launch_plus.resolver as _R
        from launch_plus.entities.state import _PackageNotFetchedError

        try:
            return _R._resolve_pkg_share(pkg)
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            if not _R._state.preview_mode:
                _R._error(f"$(find-pkg-share {pkg}): {e}")
            return f"$(find-pkg-share {pkg})"

    def perform(self, context):
        import launch_plus.resolver as _R

        pkg, is_fallback = self._resolve_name(context)
        if not is_fallback:
            _R._track_package(pkg)
        if not _R._state.preview_mode:
            return self._try_ament_resolve(pkg)
        return f"$(find-pkg-share {pkg})"

    def __str__(self):
        import launch_plus.resolver as _R

        pkg, is_fallback = self._resolve_name(None)
        if not _R._state.preview_mode:
            return self._try_ament_resolve(pkg)
        return f"$(find-pkg-share {pkg})"
