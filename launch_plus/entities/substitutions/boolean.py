"""Boolean substitutions: ``$(not ...)``, ``$(and ...)``, ``$(or ...)``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from launch_plus.entities.expose import expose_substitution
from launch_plus.entities.substitution import Substitution

if TYPE_CHECKING:
    from launch_plus.entities.state import LaunchContext

logger = logging.getLogger("launch_plus")

_BOOL_TRUE = frozenset(("true", "1"))
_BOOL_FALSE = frozenset(("false", "0"))


def _coerce_bool(value: str) -> bool:
    """Coerce a resolved string to bool, matching official ROS 2 semantics.

    Only ``"true"``/``"1"`` → True, ``"false"``/``"0"`` → False.
    Raises ``ValueError`` on anything else.
    """
    v = value.strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    raise ValueError(f"invalid boolean value: '{value}' (expected true/1/false/0)")


def _resolve_tokens(tokens: list, ctx: LaunchContext) -> str:
    """Resolve a list of substitution tokens to a string."""
    from launch_plus.entities.helpers import resolve_substitutions_from_tokens

    return resolve_substitutions_from_tokens(tokens, ctx)


@expose_substitution("not")
class NotSubstitution(Substitution):
    """``$(not value)`` → ``"true"`` or ``"false"``."""

    def __init__(self, *, value: list[Substitution]) -> None:
        self._value = value

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[NotSubstitution], dict[str, Any]]:
        if len(args) != 1:
            raise TypeError("$(not ...) expects 1 argument")
        value = args[0] if isinstance(args[0], list) else [args[0]]
        return cls, {"value": value}

    def perform(self, ctx: LaunchContext) -> str:
        resolved = _resolve_tokens(self._value, ctx)
        return str(not _coerce_bool(resolved)).lower()

    def serialize(self) -> str:
        return f"$(not {''.join(t.serialize() for t in self._value)})"


@expose_substitution("and")
class AndSubstitution(Substitution):
    """``$(and left right)`` → ``"true"`` or ``"false"``."""

    def __init__(self, *, left: list[Substitution], right: list[Substitution]) -> None:
        self._left = left
        self._right = right

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[AndSubstitution], dict[str, Any]]:
        if len(args) != 2:
            raise TypeError("$(and ...) expects 2 arguments")
        left = args[0] if isinstance(args[0], list) else [args[0]]
        right = args[1] if isinstance(args[1], list) else [args[1]]
        return cls, {"left": left, "right": right}

    def perform(self, ctx: LaunchContext) -> str:
        left = _coerce_bool(_resolve_tokens(self._left, ctx))
        right = _coerce_bool(_resolve_tokens(self._right, ctx))
        return str(left and right).lower()

    def serialize(self) -> str:
        left_s = "".join(t.serialize() for t in self._left)
        right_s = "".join(t.serialize() for t in self._right)
        return f"$(and {left_s} {right_s})"


@expose_substitution("or")
class OrSubstitution(Substitution):
    """``$(or left right)`` → ``"true"`` or ``"false"``."""

    def __init__(self, *, left: list[Substitution], right: list[Substitution]) -> None:
        self._left = left
        self._right = right

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[OrSubstitution], dict[str, Any]]:
        if len(args) != 2:
            raise TypeError("$(or ...) expects 2 arguments")
        left = args[0] if isinstance(args[0], list) else [args[0]]
        right = args[1] if isinstance(args[1], list) else [args[1]]
        return cls, {"left": left, "right": right}

    def perform(self, ctx: LaunchContext) -> str:
        left = _coerce_bool(_resolve_tokens(self._left, ctx))
        right = _coerce_bool(_resolve_tokens(self._right, ctx))
        return str(left or right).lower()

    def serialize(self) -> str:
        left_s = "".join(t.serialize() for t in self._left)
        right_s = "".join(t.serialize() for t in self._right)
        return f"$(or {left_s} {right_s})"
