"""Base class for all tracked Python-side action classes."""

from __future__ import annotations

import xml.etree.ElementTree as ET


class Action:
    """Base for all tracked Python-side action classes.

    Mirrors the official ROS 2 ``Action.execute(context)`` pattern: each
    subclass overrides ``execute()`` with its own logic.  The walker calls
    ``execute()`` and recursively walks any returned child actions.

    Actions that produce resolved output override ``serialize_resolved()``
    to return a list of ``ET.Element`` objects.
    """

    # Set by the resolver when the action is appended to resolved_actions.
    _include_chain: list[tuple[str, str]] = []

    def execute(self, context) -> list | None:
        """Execute this action. Return child actions to walk, or None."""
        return None

    def serialize_resolved(self) -> list[ET.Element]:
        """Return resolved XML elements for this action.

        Returns a list of ``xml.etree.ElementTree.Element`` objects.
        An empty list means the action produces no XML output
        (e.g. DeclareLaunchArgument, SetLaunchConfiguration).

        Subclasses that produce output override this method.
        """
        return []
