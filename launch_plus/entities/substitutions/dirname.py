"""``$(dirname)`` substitution — resolves to the current launch file's directory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.resolver import _SubstitutionContext


@expose_substitution("dirname")
class DirnameSubstitution(Substitution):
    """Resolve ``$(dirname)`` to the directory of the current launch file."""

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[DirnameSubstitution], dict[str, Any]]:
        return cls, {}

    def perform(self, ctx: _SubstitutionContext) -> str:
        if ctx.launch_file_dir:
            return ctx.launch_file_dir
        return "$(dirname)"

    def serialize(self) -> str:
        return "$(dirname)"
