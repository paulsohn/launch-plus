# Copyright 2019 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/launch_description_sources/any_launch_description_source.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""AnyLaunchDescriptionSource — deferred launch file location resolution (any format).

Matches official ``launch.launch_description_sources.AnyLaunchDescriptionSource``.
"""

from __future__ import annotations

from roscope.entities.launch_description_source import LaunchDescriptionSource


class AnyLaunchDescriptionSource(LaunchDescriptionSource):
    """Encapsulation of a launch file of any supported format, which can be loaded during launch."""

    def __init__(
        self,
        launch_file_path,
    ) -> None:
        """
        Create an AnyLaunchDescriptionSource.

        :param launch_file_path: the path to the launch file. It can be made up of Substitution
            instances which are expanded when the location is resolved.
        """
        super().__init__(launch_file_path, "interpreted launch file")
