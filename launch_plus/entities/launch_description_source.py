"""LaunchDescriptionSource — deferred location resolution.

Matches official ``launch.launch_description_sources.PythonLaunchDescriptionSource``.
"""

from __future__ import annotations

from launch_plus.entities.substitution import Substitution


class PythonLaunchDescriptionSource:
    """Stores raw location substitutions, resolves lazily.

    Matching official: ``__init__`` does NOT resolve substitutions.
    Resolution is deferred to ``_resolve_location(context)``.
    """

    def __init__(self, location=None, **kwargs):
        if location is None:
            self._location_subs = None
            self._location: str | None = None
        elif isinstance(location, str):
            self._location_subs = None
            self._location = location
        else:
            self._location_subs = location if isinstance(location, list) else [location]
            self._location = None

    def _resolve_location(self, context) -> str | None:
        """Resolve location substitutions with a real context."""
        if self._location is not None:
            return self._location
        if self._location_subs is None:
            return None
        parts = []
        for sub in self._location_subs:
            if isinstance(sub, Substitution):
                try:
                    result = sub.perform(context)
                    parts.append(str(result) if result is not None else str(sub))
                except Exception:
                    parts.append(str(sub))
            else:
                parts.append(str(sub))
        self._location = "".join(parts)
        return self._location

    @property
    def location(self) -> str | None:
        """Return location string (unresolved display if not yet resolved)."""
        if self._location is not None:
            return self._location
        if self._location_subs is None:
            return None
        return " + ".join(str(sub) for sub in self._location_subs)


class AnyLaunchDescriptionSource(PythonLaunchDescriptionSource):
    """Matches official AnyLaunchDescriptionSource."""

    pass
