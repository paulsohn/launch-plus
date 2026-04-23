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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitution.py
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/text_substitution.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Base classes for the substitution object model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext


class Substitution(ABC):
    """A single substitution token (e.g. ``$(var name)``, ``$(find-pkg-share pkg)``)."""

    @abstractmethod
    def perform(self, ctx: LaunchContext) -> str:
        """Resolve this substitution to a concrete string.

        Returns a real filesystem path or resolved value.
        """

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r})"


class TextSubstitution(Substitution):
    """Plain text with no substitution syntax."""

    def __init__(self, *, text: str) -> None:
        self.text = text

    def perform(self, ctx: LaunchContext) -> str:
        return self.text

    def __str__(self) -> str:
        return self.text
