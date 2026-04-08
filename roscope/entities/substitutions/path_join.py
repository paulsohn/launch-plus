"""PathJoinSubstitution — Python-shim substitution for path joining."""

from __future__ import annotations

from pathlib import Path

from roscope.entities.substitution import Substitution


class PathJoinSubstitution(Substitution):
    def __init__(self, substitutions):
        self._subs = substitutions

    def perform(self, context):
        parts = []
        for sub in self._subs:
            result = context.perform_substitution(sub)
            parts.append(result if result is not None else str(sub))
        return str(Path(*parts))

    def __str__(self) -> str:
        return "/".join(str(s) for s in self._subs)
