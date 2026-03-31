"""UnlessCondition — logical inverse of IfCondition."""

from __future__ import annotations

from .if_condition import IfCondition


class UnlessCondition(IfCondition):
    """Evaluate as the logical inverse of IfCondition.

    Matching official ``launch.conditions.UnlessCondition``.
    """

    def evaluate(self, context) -> bool:
        return not super().evaluate(context)
