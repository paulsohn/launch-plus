"""Base classes for the substitution object model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext


class Substitution(ABC):
    """A single substitution token (e.g. ``$(arg name)``, ``$(find-pkg-share pkg)``)."""

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
