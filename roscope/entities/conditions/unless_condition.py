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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/conditions/unless_condition.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""UnlessCondition — logical inverse of IfCondition."""

from __future__ import annotations

from .if_condition import IfCondition


class UnlessCondition(IfCondition):
    """Evaluate as the logical inverse of IfCondition.

    Matching official ``launch.conditions.UnlessCondition``.
    """

    def evaluate(self, context) -> bool:
        return not super().evaluate(context)
