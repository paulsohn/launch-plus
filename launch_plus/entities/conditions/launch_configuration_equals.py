"""LaunchConfigurationEquals / NotEquals conditions."""

from __future__ import annotations


class LaunchConfigurationEquals:
    """Evaluate whether a launch configuration equals an expected value.

    Matching official ``launch.conditions.LaunchConfigurationEquals``.
    """

    def __init__(self, name=None, expected_value=None, **kwargs):
        self._name = name
        self._expected = expected_value

    def evaluate(self, context) -> bool:
        try:
            val = context._launch_configurations.get(str(self._name), "")
            return str(val) == str(self._expected)
        except Exception:
            return False


class LaunchConfigurationNotEquals(LaunchConfigurationEquals):
    """Evaluate whether a launch configuration does NOT equal an expected value.

    Matching official ``launch.conditions.LaunchConfigurationNotEquals``.
    """

    def evaluate(self, context) -> bool:
        return not super().evaluate(context)
