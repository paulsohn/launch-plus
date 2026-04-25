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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/unset_environment_variable.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the UnsetEnvironmentVariable action."""

from __future__ import annotations

import logging

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _current_file, resolve_value
from roscope.entities.parsing import Parser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("unset_env")
class UnsetEnvironmentVariable(Action):
    """Action that unsets an environment variable if it is set, otherwise does nothing."""

    def __init__(self, name=None, **kwargs):
        super().__init__(**kwargs)
        self._name = name

    @classmethod
    def parse(cls, entity: Entity, parser: Parser):
        _, kwargs = super().parse(entity, parser)
        kwargs["name"] = parser.parse_substitution(entity.get_attr("name"))
        return cls, kwargs

    def execute(self, context) -> list | None:
        name = resolve_value(self._name, context)
        if name in context.environment:
            del context.environment[name]
        else:
            logger.warning(
                "%s: UnsetEnvironmentVariable: environment variable '%s' is not set. Skipping.",
                _current_file(context),
                name,
            )
        return None
