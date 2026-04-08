"""LaunchDescription — container for launch entities.

Matches official ``launch.LaunchDescription``.
"""

from __future__ import annotations


class LaunchDescription:
    """Container for launch entities (actions, substitutions, etc.)."""

    def __init__(self, entities=None, **kwargs):
        self.entities = list(entities or [])

    def add_action(self, action):
        self.entities.append(action)
