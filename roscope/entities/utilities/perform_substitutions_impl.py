"""Module for the perform_substitutions() utility function."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext
    from roscope.entities.substitution import Substitution


def perform_substitutions(context: LaunchContext, subs: list[Substitution]) -> str:
    """Resolve a list of Substitutions with a context into a single string."""
    return "".join([context.perform_substitution(sub) for sub in subs])
