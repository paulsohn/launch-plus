"""Base class for all tracked Python-side action classes."""

from __future__ import annotations

from xml.sax.saxutils import escape as _xml_escape


class Action:
    """Base for all tracked Python-side action classes.

    Mirrors the official ROS 2 ``Action.execute(context)`` pattern: each
    subclass overrides ``execute()`` with its own logic.  The walker calls
    ``execute()`` and recursively walks any returned child actions.

    Actions that produce resolved output override ``serialize_resolved()``
    to return an XML snippet.
    """

    # Set by the resolver when the action is appended to resolved_actions.
    _include_chain: list[tuple[str, str]] = []

    def execute(self, context) -> list | None:
        """Execute this action. Return child actions to walk, or None."""
        return None

    def serialize_resolved(self, indent: str = "  ") -> str | None:
        """Return the resolved XML snippet for this action.

        Returns ``None`` if the action should not appear in the resolved
        output (e.g. DeclareLaunchArgument, SetLaunchConfiguration).

        Subclasses that produce output override this method.
        """
        return None

    @staticmethod
    def _esc(value: str) -> str:
        """XML-escape a string value, including quotes for attribute safety."""
        return _xml_escape(value, {'"': "&quot;"})
