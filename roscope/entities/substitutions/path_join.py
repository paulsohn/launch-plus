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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/path_join_substitution.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""PathJoinSubstitution — Python-shim substitution for path joining."""

from __future__ import annotations

from pathlib import Path

from roscope.entities.substitution import Substitution


class PathJoinSubstitution(Substitution):
    def __init__(self, substitutions):
        self._subs = substitutions

    def perform(self, context):
        parts = []
        for sub in self._subs:
            result = context.perform_substitution(sub)
            parts.append(result if result is not None else str(sub))
        return str(Path(*parts))

    def __str__(self) -> str:
        return "/".join(str(s) for s in self._subs)
