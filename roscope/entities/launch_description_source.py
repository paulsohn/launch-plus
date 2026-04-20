"""LaunchDescriptionSource — base class for launch description sources.

Matches official ``launch.launch_description_source.LaunchDescriptionSource``.
"""

from __future__ import annotations

from roscope.entities.substitution import Substitution


class LaunchDescriptionSource:
    """Stores raw location substitutions, resolves lazily.

    Matching official: ``__init__`` does NOT resolve substitutions.
    Resolution is deferred to ``_resolve_location(context)``.
    """

    def __init__(
        self,
        location=None,
        method: str = "unspecified mechanism from a script",
        **_kwargs,
    ) -> None:
        self._method = method
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

    @property
    def method(self) -> str:
        """Getter for method."""
        return self._method
