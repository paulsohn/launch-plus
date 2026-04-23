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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/launch_description_sources/__init__.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Launch description source implementations."""

from roscope.entities.launch_description_sources.any_launch_description_source import (
    AnyLaunchDescriptionSource,
)
from roscope.entities.launch_description_sources.frontend_launch_description_source import (
    FrontendLaunchDescriptionSource,
)
from roscope.entities.launch_description_sources.python_launch_description_source import (
    PythonLaunchDescriptionSource,
)
from roscope.entities.launch_description_sources.xml_launch_description_source import (
    XMLLaunchDescriptionSource,
)

__all__ = [
    "AnyLaunchDescriptionSource",
    "FrontendLaunchDescriptionSource",
    "PythonLaunchDescriptionSource",
    "XMLLaunchDescriptionSource",
]
