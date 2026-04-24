# Copyright 2020 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/anon_name.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""``$(anon name)`` substitution — generates a deterministic anonymous name.

Mirrors the official ``AnonName`` substitution semantics:
- Same *name* within the same launch context always yields the same result.
- Different *names* yield different results.

The official implementation appends hostname/PID/random to make names unique
at runtime.  roscope is a static resolver, so it uses a short deterministic
hash instead — the anonymised name is stable across runs and still unique per
base name.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext


@expose_substitution("anon")
class AnonName(Substitution):
    """Resolve ``$(anon name)`` to a stable anonymised identifier."""

    def __init__(self, name: list[Substitution]) -> None:
        self._name = name

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[AnonName], dict[str, Any]]:
        if len(args) != 1:
            raise TypeError("anon substitution expects 1 argument")
        return cls, {"name": args[0]}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.utilities import perform_substitutions

        name = perform_substitutions(ctx, self._name)

        # Official stores per-name cache as flat keys "anon" + name in launch_configurations.
        # e.g. $(anon my_node) stores the result under "anonmy_node".
        key = "anon" + name
        if key not in ctx._launch_configurations:
            suffix = hashlib.md5(name.encode()).hexdigest()[:8]
            ctx._launch_configurations[key] = f"{name}_{suffix}"

        return str(ctx._launch_configurations[key])

    def __str__(self) -> str:
        try:
            name = "".join(str(s) for s in self._name)
        except Exception:
            name = repr(self._name)
        return f"$(anon {name})"
