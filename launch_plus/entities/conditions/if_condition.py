"""IfCondition — evaluates a substitution as a boolean."""

from __future__ import annotations

from launch_plus.entities.substitution import Substitution


class IfCondition:
    """Evaluate a substitution as a boolean condition.

    Matching official ``launch.conditions.IfCondition``.
    """

    def __init__(self, condition=None, **kwargs):
        self._condition = condition

    def evaluate(self, context) -> bool:
        try:
            if isinstance(self._condition, Substitution):
                val = str(self._condition.perform(context)).lower()
                return val in ("true", "1")
            if self._condition is None:
                return False
            return bool(self._condition)
        except Exception:
            return False
