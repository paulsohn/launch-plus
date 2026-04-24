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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/this_launch_file_dir.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""``$(dirname)`` substitution — resolves to the current launch file's directory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext


@expose_substitution("dirname")
class DirnameSubstitution(Substitution):
    """Resolve ``$(dirname)`` to the directory of the current launch file."""

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[DirnameSubstitution], dict[str, Any]]:
        return cls, {}

    def perform(self, ctx: LaunchContext) -> str:
        if ctx.launch_file_dir:
            return ctx.launch_file_dir
        return "$(dirname)"

    def __str__(self) -> str:
        return "$(dirname)"
