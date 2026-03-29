"""PathJoinSubstitution — Python-shim substitution for path joining."""

from __future__ import annotations

from pathlib import Path


class _TrackedPathJoinSubstitution:
    def __init__(self, substitutions):
        import launch_plus.resolver as _R

        self._subs = substitutions
        # Track packages from nested FindPackageShare
        for sub in substitutions:
            if hasattr(sub, "_resolve_name"):
                pkg, is_fallback = sub._resolve_name()
                if not is_fallback:
                    _R._track_package(pkg)

    def perform(self, context):
        parts = []
        for sub in self._subs:
            if hasattr(sub, "perform"):
                result = sub.perform(context)
                parts.append(str(result) if result is not None else str(sub))
            else:
                parts.append(str(sub))
        return str(Path(*parts))
