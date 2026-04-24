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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/timer_action.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the TimerAction action."""

from __future__ import annotations

import logging

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _current_file
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("timer")
class TimerAction(Action):
    """TimerAction cannot be statically resolved.

    Timer-based actions depend on runtime timing which is not sequential
    and cannot be determined during static analysis.
    """

    def __init__(self, *, period, actions, **kwargs):
        super().__init__(**kwargs)
        self.period = period
        self.actions: list = list(actions or [])

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        """Return the `Timer` action and kwargs for constructing it."""
        _, kwargs = super().parse(entity, parser)

        period_raw = entity.get_attr("period", optional=True) or "0.0"
        kwargs["period"] = parser.parse_substitution(period_raw)
        kwargs["actions"] = list(entity.children)
        return cls, kwargs

    def execute(self, context) -> list:
        logger.error(
            "%s: TimerAction cannot be statically resolved: "
            "context execution timing is not sequential",
            _current_file(context),
        )
        return []
