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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/launch_description_sources/frontend_launch_description_source.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""FrontendLaunchDescriptionSource — deferred declarative launch file location resolution.

Matches official ``launch.launch_description_sources.FrontendLaunchDescriptionSource``.
"""

from __future__ import annotations

from roscope.entities.launch_description_source import LaunchDescriptionSource


class FrontendLaunchDescriptionSource(LaunchDescriptionSource):
    """Encapsulation of a declarative (markup-based) launch file."""

    def __init__(
        self,
        launch_file_path,
        *,
        method: str = "interpreted frontend launch file",
        parser=None,
    ) -> None:
        """
        Create a FrontendLaunchDescriptionSource.

        :param launch_file_path: the path to the launch file. It can be made up of Substitution
            instances which are expanded when the location is resolved.
        :param method: human-readable description of how the launch description is generated.
        :param parser: stored for API compatibility with the official
            ``FrontendLaunchDescriptionSource``. Not used for dispatch — the
            resolver selects a parser by file extension instead.
        """
        super().__init__(launch_file_path, method)
        self._parser = parser
