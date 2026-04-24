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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/utilities/perform_substitutions_impl.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the perform_substitutions() utility function."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext
    from roscope.entities.substitution import Substitution


def perform_substitutions(context: LaunchContext, subs: list[Substitution]) -> str:
    """Resolve a list of Substitutions with a context into a single string."""
    return "".join([context.perform_substitution(sub) for sub in subs])
