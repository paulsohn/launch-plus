"""Module for SomeSubstitutionsType type."""

import collections.abc
from collections.abc import Iterable
from pathlib import Path

from roscope.entities.substitution import Substitution

SomeSubstitutionsType = str | Path | Substitution | Iterable[str | Path | Substitution]

SomeSubstitutionsType_types_tuple = (
    str,
    Path,
    Substitution,
    collections.abc.Iterable,
)
