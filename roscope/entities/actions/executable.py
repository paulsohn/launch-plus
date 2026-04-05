"""Action handler for <executable> / ExecuteProcess.

Matching official ``launch.actions.ExecuteProcess``.
Method definition order follows the official implementation.
"""

from __future__ import annotations

import shlex
import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.entities.substitution import Substitution, TextSubstitution
from roscope.entities.utilities import normalize_to_list_of_substitutions, perform_substitutions
from roscope.parsers.entity import Entity


@expose_action("executable")
class ExecuteProcess(Action):
    """Tracks an ExecuteProcess / <executable>.

    Matching official: ``cmd`` is stored as ``list[list[Substitution]]``
    where each inner list represents one command argument.
    """

    def __init__(self, *, cmd=None, name=None, **kwargs):
        # Normalize cmd: list of argument lists, matching official Executable
        if cmd is None:
            self.cmd: list[list[Substitution]] | str = []
        elif isinstance(cmd, str):
            # Already resolved string (from resolved object)
            self.cmd = cmd
        elif isinstance(cmd, list) and cmd and isinstance(cmd[0], list):
            # Already list[list[Substitution]] (from parse)
            self.cmd = cmd
        else:
            # From Python shim: list of mixed str/Substitution items
            self.cmd = [normalize_to_list_of_substitutions(x) for x in cmd]
        self.name = normalize_to_list_of_substitutions(name) if name is not None else name
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

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        cmd_raw = entity.get_attr("cmd", optional=True) or ""
        name_raw = entity.get_attr("name", optional=True)
        return cls(
            cmd=cls._parse_cmdline(cmd_raw, parser),
            name=parser.parse_substitution(name_raw) if name_raw else None,
            additional_env=parser.parse_envs(entity),
        )

    def execute(self, context) -> list:
        """Resolve substitutions and return a clean resolved ExecuteProcess."""
        from roscope.entities.helpers import env_overrides, resolve_value

        cmd_parts = (
            [perform_substitutions(context, arg) for arg in self.cmd]
            if isinstance(self.cmd, list)
            else [self.cmd]
        )
        name = (
            perform_substitutions(context, self.name)
            if self.name is not None and not isinstance(self.name, str)
            else self.name
        )

        env = env_overrides(context)
        if self.additional_env is not None:
            for k_tokens, v_tokens in self.additional_env:
                env[resolve_value(k_tokens, context) or ""] = resolve_value(v_tokens, context) or ""

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
        return [elem]
