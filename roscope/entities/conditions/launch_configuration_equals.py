# Copyright 2020 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/conditions/launch_configuration_equals.py
# - https://github.com/ros2/launch/blob/rolling/launch/launch/conditions/launch_configuration_not_equals.py
# Modified for roscope project by Taeseung Sohn, 2026.

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
