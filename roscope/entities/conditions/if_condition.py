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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/conditions/if_condition.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""IfCondition — evaluates a substitution as a boolean."""

from __future__ import annotations

from roscope.entities.substitution import Substitution


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
