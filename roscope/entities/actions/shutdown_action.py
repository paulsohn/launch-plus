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

# Originally from (planned to split and refactor):
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/shutdown_event.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the Shutdown action."""

from __future__ import annotations

from roscope.entities.actions.emit_event import EmitEvent
from roscope.entities.expose import expose_action


@expose_action("shutdown")
class Shutdown(EmitEvent):
    """Action that shuts down a launched system by emitting Shutdown when executed. (TODO)"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
