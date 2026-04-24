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

# Originally from (planned to split and refactor):
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/register_event_handler.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the RegisterEventHandler action."""

from __future__ import annotations

import logging

from roscope.entities.action import Action
from roscope.entities.helpers import _current_file

logger = logging.getLogger("roscope")


class RegisterEventHandler(Action):
    """
    Action that registers an event handler.

    Event-handler-driven topology changes are not resolved — the resolved output
    will not include nodes or actions that are only launched in response to an
    event.  A warning is emitted so that users are aware of this gap.
    """

    def __init__(self, event_handler=None, **kwargs):
        super().__init__(**kwargs)
        self._event_handler = event_handler

    def execute(self, context) -> list:
        logger.warning(
            "%s: RegisterEventHandler encountered — event-handler-driven topology "
            "is not resolved; nodes or actions triggered by events will not "
            "appear in the resolved output",
            _current_file(context),
        )
        return []
