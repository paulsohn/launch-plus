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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/execute_process.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Action handler for <executable> / ExecuteProcess.

Matching official ``launch.actions.ExecuteProcess``.
Method definition order follows the official implementation.
"""

from __future__ import annotations

import logging
import shlex
import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _current_file, env_overrides, resolve_value
from roscope.entities.parsing import _ActionParser
from roscope.entities.substitution import Substitution, TextSubstitution
from roscope.entities.utilities import normalize_to_list_of_substitutions, perform_substitutions
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("executable")
class ExecuteProcess(Action):
    """Tracks an ExecuteProcess / <executable>.

    Matching official: ``cmd`` is stored as ``list[list[Substitution]]``
    where each inner list represents one command argument.
    """

    def __init__(self, *, cmd=None, name=None, condition=None, **kwargs):
        super().__init__(condition=condition)
        # Normalize cmd: list of argument lists, matching official Executable
        if isinstance(cmd, str):
            # Already resolved string (from resolved object)
            self.cmd = cmd
        elif isinstance(cmd, list) and cmd and isinstance(cmd[0], list):
            # Already list[list[Substitution]] (from parse)
            self.cmd = cmd
        else:
            # From Python shim: list of mixed str/Substitution items
            self.cmd = [normalize_to_list_of_substitutions(x) for x in cmd]
        # str → already resolved; anything else → normalize to list[Substitution]
        if name is None or isinstance(name, str):
            self.name = name
        else:
            self.name = normalize_to_list_of_substitutions(name)
        self.additional_env = kwargs.pop("additional_env", None)
        self.env: dict = {}

    @classmethod
    def _parse_cmdline(cls, cmd: str, parser: _ActionParser) -> list[list[Substitution]]:
        """Parse text apt for command line execution.

        Matching official ``ExecuteProcess._parse_cmdline``: splits on
        whitespace boundaries while preserving substitutions.
        """
        result_args: list[list[Substitution]] = []
        arg: list[Substitution] = []

        def _append_arg() -> None:
            nonlocal arg
            result_args.append(arg)
            arg = []

        for sub in parser.parse_substitution(cmd):
            if isinstance(sub, TextSubstitution):
                tokens = shlex.split(sub.text)
                if not tokens:
                    # String with just spaces — appending args allows splitting two
                    # substitutions separated by a space (matches official behavior).
                    _append_arg()
                    continue
                if sub.text[0].isspace():  # noqa: SIM102 — matches official
                    if len(arg) != 0:
                        _append_arg()
                arg.append(TextSubstitution(text=tokens[0]))
                if len(tokens) > 1:
                    _append_arg()
                    arg.append(TextSubstitution(text=tokens[-1]))
                if len(tokens) > 2:
                    result_args.extend([TextSubstitution(text=x)] for x in tokens[1:-1])
                if sub.text[-1].isspace():
                    _append_arg()
            else:
                arg.append(sub)
        if arg:
            result_args.append(arg)
        return result_args

    @staticmethod
    def parse_envs(entity: Entity, parser: _ActionParser) -> dict:
        """Extract <env> children as a dict of unresolved token lists."""
        items = entity.get_attr("env", data_type=list, optional=True)
        if not items:
            return {}
        result = {}
        for e in items:
            name_raw = e.get_attr("name", optional=True) or ""
            if not name_raw.strip():
                logger.error(
                    "%s: skipping <env> child with missing or empty name attribute",
                    _current_file(parser.ctx),
                )
                continue
            result[tuple(parser.parse_substitution(name_raw))] = parser.parse_substitution(
                e.get_attr("value", optional=True) or ""
            )
        return result

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser, ignore: list | None = None):
        _, kwargs = super().parse(entity, parser)
        ignore = ignore or []
        if "cmd" not in ignore:
            cmd_raw = entity.get_attr("cmd", optional=True) or ""
            kwargs["cmd"] = cls._parse_cmdline(cmd_raw, parser)
        name_raw = entity.get_attr("name", optional=True)
        kwargs["name"] = parser.parse_substitution(name_raw) if name_raw else None
        kwargs["additional_env"] = cls.parse_envs(entity, parser)
        return cls, kwargs

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved ExecuteProcess."""
        cmd_parts = (
            [perform_substitutions(context, arg) for arg in self.cmd]
            if isinstance(self.cmd, list)
            else [self.cmd]
        )
        name = context.perform_substitution(self.name) if self.name is not None else None

        env = env_overrides(context)
        if self.additional_env is not None:
            for k_tokens, v_tokens in self.additional_env.items():
                k = resolve_value(k_tokens, context) or ""
                if not k:
                    logger.error(
                        "%s: additional_env entry has an empty variable name; skipping",
                        _current_file(context),
                    )
                    continue
                env[k] = resolve_value(v_tokens, context) or ""
        resolved = ExecuteProcess(cmd=" ".join(cmd_parts), name=name)
        resolved.env = env
        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.cmd:
            return []
        elem = ET.Element("executable")
        elem.set("cmd", self.cmd if isinstance(self.cmd, str) else "")
        if self.name:
            elem.set("name", self.name if isinstance(self.name, str) else "")
        for k, v in sorted((self.env or {}).items()):
            e = ET.SubElement(elem, "env")
            e.set("name", k)
            e.set("value", v)
        return [elem]
