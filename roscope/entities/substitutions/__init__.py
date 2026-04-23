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

# Originally from (planned to refactor):
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/__init__.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Substitution handlers (registered via @expose_substitution)."""

import roscope.entities.substitutions.anon_name  # noqa: F401
import roscope.entities.substitutions.boolean  # noqa: F401
import roscope.entities.substitutions.command  # noqa: F401
import roscope.entities.substitutions.dirname  # noqa: F401
import roscope.entities.substitutions.env  # noqa: F401
import roscope.entities.substitutions.equals  # noqa: F401
import roscope.entities.substitutions.eval  # noqa: F401
import roscope.entities.substitutions.executable_in_package  # noqa: F401
import roscope.entities.substitutions.find_pkg_prefix  # noqa: F401
import roscope.entities.substitutions.find_pkg_share  # noqa: F401
import roscope.entities.substitutions.if_else  # noqa: F401
import roscope.entities.substitutions.launch_config  # noqa: F401
