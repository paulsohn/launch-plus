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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/opaque_function.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the OpaqueFunction action."""

from __future__ import annotations

import logging

from roscope.entities.action import Action
from roscope.entities.helpers import _current_file

logger = logging.getLogger("roscope")


def visit_actions(actions, context) -> list:
    """Execute a list of actions and collect resolved results."""
    results: list = []
    for action in actions or []:
        if action is None:
            continue
        if not isinstance(action, Action):
            logger.error(
                "%s: expected Action, got %s", _current_file(context), type(action).__name__
            )
            continue
        children = action.visit(context)
        if children:
            results.extend(children)
    return results


class OpaqueFunction(Action):
    """Action that executes a Python function."""

    def __init__(self, *, function, args=None, kwargs=None, **left_over_kwargs):
        super().__init__(**left_over_kwargs)
        self.function = function
        self.args = args or []
        self.kwargs = kwargs or {}

    def execute(self, context) -> list:
        try:
            result = self.function(context, *self.args, **self.kwargs) or []

            return visit_actions(result, context)
        except Exception as e:
            logger.error(
                "OpaqueFunction failed in %s: %s",
                _current_file(context),
                e,
                exc_info=True,
            )

        return []
