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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/utilities/normalize_to_list_of_substitutions_impl.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the normalize_to_list_of_substitutions() utility function."""

from __future__ import annotations

from pathlib import Path

from roscope.entities.some_substitutions_type import SomeSubstitutionsType
from roscope.entities.substitution import Substitution, TextSubstitution


def normalize_to_list_of_substitutions(subs: SomeSubstitutionsType) -> list[Substitution]:
    """Return a list of Substitutions given a variety of starting inputs.

    Accepts:
    - A single ``str`` or ``Path`` → ``[TextSubstitution(text=str)]``
    - A single ``Substitution`` → ``[sub]``
    - An iterable of ``str``/``Substitution`` → normalized list
    """
    if isinstance(subs, Substitution):
        return [subs]
    if isinstance(subs, (str, Path)):
        return [TextSubstitution(text=str(subs))]
    return [_normalize_one(s) for s in subs]


def _normalize_one(x) -> Substitution:
    if isinstance(x, Substitution):
        return x
    if isinstance(x, (str, Path)):
        return TextSubstitution(text=str(x))
    raise TypeError(
        f"Failed to normalize item of type '{type(x)}', expected 'str' or 'Substitution'"
    )
