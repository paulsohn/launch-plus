"""Registration decorators and dispatch for actions and substitutions.

Adapted from ROS 2 ``launch.frontend.expose``.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from launch_plus.entities.substitution import Substitution

# ── Global registries ────────────────────────────────────────────────────────

action_parse_methods: dict[str, Callable[..., Any]] = {}
"""``{tag_name: parse_method}`` for actions (``arg``, ``node``, …)."""

substitution_parse_methods: dict[str, Callable[..., tuple[type[Substitution], dict[str, Any]]]] = {}
"""``{subst_name: parse_method}`` for substitutions (``arg``, ``var``, …)."""


# ── Instantiation helpers ────────────────────────────────────────────────────


def instantiate_substitution(
    type_name: str,
    args: Any = None,
) -> Substitution:
    """Look up *type_name* in the substitution registry and return a new instance.

    *args* is a list of "values" from the Lark grammar — each value is
    itself a list of :class:`Substitution` objects representing one
    space-separated argument to the substitution expression.
    """
    if type_name not in substitution_parse_methods:
        raise RuntimeError(f"Unknown substitution: {type_name}")
    args = args if args is not None else []
    subst_type, kwargs = substitution_parse_methods[type_name](args)
    return subst_type(**kwargs)


# ── Decorator internals ──────────────────────────────────────────────────────


def _expose_impl(
    name: str,
    registry: dict[str, Any],
    exposed_type: str,
) -> Callable[..., Any]:
    """Return a decorator that registers *name* → ``cls.parse`` (or a bare callable)."""

    def decorator(exposed: Any) -> Any:
        parse_method: Callable[..., Any] | None = None

        if inspect.isclass(exposed):
            if hasattr(exposed, "parse") and callable(exposed.parse):
                parse_method = exposed.parse
            else:
                raise RuntimeError(
                    f"Class decorated with @expose_{exposed_type}('{name}') "
                    "must define a classmethod 'parse'."
                )
        elif callable(exposed):
            parse_method = exposed
        else:
            raise RuntimeError(
                f"@expose_{exposed_type}('{name}'): target is not a class or callable"
            )

        if name in registry and registry[name] is not parse_method:
            raise RuntimeError(
                f"Two {exposed_type} parsing methods exposed with the same name: [{name}]"
            )

        if exposed_type == "action":

            @functools.wraps(parse_method)
            def wrapper(entity: Any, parser: Any) -> None:
                parse_method(entity, parser)
                try:
                    entity.assert_entity_completely_parsed()
                except ValueError:
                    pass  # Tolerate unconsumed attrs (e.g. condition-false early return)

            registry[name] = wrapper
        else:
            registry[name] = parse_method

        return exposed

    return decorator


def expose_action(name: str) -> Callable[..., Any]:
    """Decorator: register an action parser for *name* (e.g. ``'node'``)."""
    return _expose_impl(name, action_parse_methods, "action")


def expose_substitution(name: str) -> Callable[..., Any]:
    """Decorator: register a substitution parser for *name* (e.g. ``'arg'``)."""
    return _expose_impl(name, substitution_parse_methods, "substitution")
