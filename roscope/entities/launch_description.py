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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/launch_description.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""LaunchDescription — container for launch entities.

Matches official ``launch.LaunchDescription``.
"""

from __future__ import annotations


class LaunchDescription:
    """Container for launch entities (actions, substitutions, etc.)."""

    def __init__(self, entities=None, **kwargs):
        self.entities = list(entities or [])

    def add_action(self, action):
        self.entities.append(action)
