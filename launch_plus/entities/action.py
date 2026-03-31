"""Base class for all tracked Python-side action classes."""

from __future__ import annotations


class Action:
    """Base for all tracked Python-side action classes.

    Mirrors the official ROS 2 ``Action.execute(context)`` pattern: each
    subclass overrides ``execute()`` with its own logic.  The walker calls
    ``execute()`` and recursively walks any returned child actions.
    """

    def execute(self, context) -> list | None:
        """Execute this action. Return child actions to walk, or None."""
        return None
