"""Base class for all tracked Python-side action classes."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from roscope.entities.parsing import _ActionParser
    from roscope.parsers.entity import Entity


class Action:
    """Base for all tracked Python-side action classes.

    Mirrors the official ROS 2 ``Action`` pattern:

    - ``parse(entity, parser)`` builds an unresolved action (class method).
    - ``execute(context)`` performs side effects and returns child actions.
    - ``visit(context)`` evaluates the optional condition and calls
      ``execute()`` only when the condition is true — matching official
      ``Action.visit()`` exactly.

    Actions that produce resolved output override ``serialize_resolved()``
    to return a list of ``ET.Element`` objects.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser) -> tuple[type, dict[str, Any]]:
        """Parse if=/unless= and return a (cls, kwargs) base.

        Matches official ``Action.parse()`` which extracts the condition and
        returns ``(cls, kwargs)`` for subclasses to extend via ``super().parse()``.
        Subclasses call ``_, kwargs = super().parse(entity, parser)`` and add
        their own kwargs before returning ``cls, kwargs``.
        """
        condition = parser.parse_condition(entity)
        kwargs = {}
        if condition is not None:
            kwargs["condition"] = condition
        return cls, kwargs

    def __init__(self, *, condition=None, **kwargs) -> None:
        """Create an Action.

        :param condition: A :class:`~roscope.entities.parsing._ParsedCondition`
            (or any object with ``evaluate(context) -> bool``), or ``None``
            to always execute.
        """
        self._condition = condition

    def visit(self, context) -> list | None:
        """Evaluate condition and execute if true.

        Matches official ``Action.visit()``:
        - If no condition: always execute.
        - If condition evaluates to True: execute.
        - Otherwise: return None (skip).
        """
        if self._condition is None or self._condition.evaluate(context):
            return self.execute(context)
        return None

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
