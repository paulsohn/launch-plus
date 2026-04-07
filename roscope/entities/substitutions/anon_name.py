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
    from roscope.entities.state import LaunchContext


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

        anon_map: dict[str, str] = ctx._state.tracked.setdefault("anon_names", {})
        if name not in anon_map:
            suffix = hashlib.md5(name.encode()).hexdigest()[:8]
            anon_map[name] = f"{name}_{suffix}"

        return anon_map[name]

    def __str__(self) -> str:
        try:
            name = "".join(str(s) for s in self._name)
        except Exception:
            name = repr(self._name)
        return f"$(anon {name})"
