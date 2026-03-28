"""Base classes for the substitution object model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from launch_plus.resolver import _SubstitutionContext


class Substitution(ABC):
    """A single substitution token (e.g. ``$(arg name)``, ``$(find-pkg-share pkg)``)."""

    @abstractmethod
    def perform(self, ctx: _SubstitutionContext, *, _depth: int = 0) -> str:
        """Resolve this substitution to a concrete string.

        In preview mode the result should be portable (e.g. ``$(find-pkg-share pkg)``).
        """

    @abstractmethod
    def serialize(self) -> str:
        """Return the canonical portable form, always (regardless of preview mode)."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.serialize()!r})"


class TextSubstitution(Substitution):
    """Plain text with no substitution syntax."""

    def __init__(self, *, text: str) -> None:
        self.text = text

    def perform(self, ctx: _SubstitutionContext, *, _depth: int = 0) -> str:
        return self.text

    def serialize(self) -> str:
        return self.text
