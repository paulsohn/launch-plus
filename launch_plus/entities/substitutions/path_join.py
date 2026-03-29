"""PathJoinSubstitution — Python-shim substitution for path joining."""

from __future__ import annotations

from pathlib import Path


class _TrackedPathJoinSubstitution:
    def __init__(self, substitutions):
        self._subs = substitutions

    def perform(self, context):
        parts = []
        for sub in self._subs:
            if hasattr(sub, "perform"):
                result = sub.perform(context)
                parts.append(str(result) if result is not None else str(sub))
            else:
                parts.append(str(sub))
        return str(Path(*parts))
