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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/frontend/entity.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Entity base class — intermediate representation for parsed launch elements.

Both :class:`XmlEntity` and :class:`YamlEntity` implement this interface,
allowing downstream resolution logic to be written once.

Adapted from ROS 2 ``launch.frontend.entity``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any


class Entity(ABC):
    """A single item in the intermediate frontend representation.

    Produced by XML and YAML parsers.  Consumed by action handlers (Phase 3)
    or converted to the legacy ``dict[str, Any]`` format via :meth:`to_dict`.
    """

    @property
    @abstractmethod
    def type_name(self) -> str:
        """Element type, e.g. ``'node'``, ``'arg'``, ``'include'``."""

    @property
    @abstractmethod
    def parent(self) -> Entity | None:
        """Parent entity, or ``None`` for root-level elements."""

    @property
    @abstractmethod
    def children(self) -> Sequence[Entity]:
        """Direct child entities."""

    @abstractmethod
    def get_attr(
        self,
        name: str,
        *,
        data_type: type = str,
        optional: bool = False,
    ) -> Any:
        """Read an attribute by name.

        For XML entities, this reads from element attributes with type
        coercion.  For YAML entities, this reads from dict keys with
        type checking.

        When *data_type* is ``list``, returns child entities matching
        *name* as a tag (XML) or key (YAML).

        :raises AttributeError: if not *optional* and the attribute is missing.
        :raises TypeError: if the value cannot be coerced/checked to *data_type*.
        """

    @abstractmethod
    def assert_entity_completely_parsed(self) -> None:
        """Verify that all attributes and children were consumed.

        Called automatically after an action's ``parse()`` method completes.
        Raises :class:`ValueError` if any attributes or children were not
        accessed via :meth:`get_attr` or :attr:`children`.
        """
