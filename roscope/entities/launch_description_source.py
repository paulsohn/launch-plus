# Copyright 2018 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Originally from:
# - https://github.com/ros2/launch/blob/rolling/launch/launch/launch_description_source.py
# Modified for roscope project by Taeseung Sohn, 2026.

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
