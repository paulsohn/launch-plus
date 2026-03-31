"""Module for the normalize_to_list_of_substitutions() utility function."""

from __future__ import annotations

from pathlib import Path

from launch_plus.entities.substitution import Substitution, TextSubstitution


def normalize_to_list_of_substitutions(subs) -> list[Substitution]:
    """Return a list of Substitutions given a variety of starting inputs.

    Accepts:
    - A single ``str`` or ``Path`` → ``[TextSubstitution(text=str)]``
    - A single ``Substitution`` → ``[sub]``
    - An iterable of ``str``/``Substitution`` → normalized list
    """
    if isinstance(subs, Substitution):
        return [subs]
    if isinstance(subs, (str, Path)):
        return [TextSubstitution(text=str(subs))]
    return [_normalize_one(s) for s in subs]


def _normalize_one(x) -> Substitution:
    if isinstance(x, Substitution):
        return x
    if isinstance(x, (str, Path)):
        return TextSubstitution(text=str(x))
    raise TypeError(
        f"Failed to normalize item of type '{type(x)}', expected 'str' or 'Substitution'"
    )
