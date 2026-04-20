"""Pure helper functions extracted from resolver.py.

These functions track packages, nodes, includes, parameters, and event
handlers in :class:`~roscope.entities.state.ResolverState`.  They are
intentionally stateless with respect to module-level variables — every
function receives the state object explicitly.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from roscope.entities.state import LaunchContext

logger = logging.getLogger("roscope")


def env_overrides(context: LaunchContext) -> dict[str, str]:
    """Return env vars explicitly set via SetEnvironmentVariable (overrides only).

    Compares ``context.environment`` against ``os.environ`` to extract only
    the diff.  Used by node resolution to capture per-node env in output.
    """
    overrides = {}
    for k, v in context.environment.items():
        if k not in os.environ or os.environ[k] != v:
            overrides[k] = v
    return overrides


if TYPE_CHECKING:
    from roscope.entities.substitution import Substitution as _SubstitutionType

# ─── Portable path support ────────────────────────────────────────────────────


def _is_substitution(value):
    """Return True if *value* is a launch substitution (not yet resolved to a string).

    Handles both single substitution objects (with a ``perform`` method) and
    list-of-substitutions (``SomeSubstitutionsType`` in ROS 2), where any
    element may be a substitution object.
    """
    from roscope.entities.substitution import Substitution

    if value is None or isinstance(value, str):
        return False
    if isinstance(value, Substitution):
        return True
    if isinstance(value, (list, tuple)):
        return any(isinstance(item, Substitution) for item in value)
    return False


def _extract_pkg_and_share_path(path_str: str):
    """Extract (package, share_path) from an install path.

    Matches: ``/opt/.../share/pkg/launch/foo.py`` → ``("pkg", "launch/foo.py")``

    Returns ``None`` if the path doesn't match the ``/share/<pkg>/`` pattern.
    """
    idx = path_str.find("/share/")
    if idx >= 0:
        after_share = path_str[idx + 7 :]  # skip "/share/"
        slash = after_share.find("/")
        if slash > 0:
            pkg = after_share[:slash]
            rest = after_share[slash + 1 :]
            if rest:
                return pkg, rest
    return None


def _to_str(value: object, context: Any = None) -> str | None:
    """Coerce a str, substitution object, or None to ``str | None``.

    - ``None`` → ``None`` (caller decides how to handle missing values)
    - ``str``  → returned as-is
    - object with ``.perform()`` → call it; ``None`` result stays ``None``,
      non-``None`` result is coerced to ``str``; on exception → ``str(value)``
    - anything else → ``str(value)``

    Every non-``None`` return value is guaranteed to be ``str``.
    """
    from roscope.entities.substitution import Substitution

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Substitution):
        try:
            res = value.perform(context)
            return str(res) if res is not None else None
        except Exception:
            return str(value)
    return str(value)


# ─── Substitution Engine (for XML/YAML resolution) ───────────────────────────
#
# Parses and resolves ROS 2 substitution syntax: $(var x), $(env Y default),
# $(find-pkg-share pkg), $(dirname), $(eval expr), $(command ...).
# Used by the XML/YAML element walker — Python launch files use the
# existing .perform() mechanism instead.


def resolve_substitutions_from_tokens(
    tokens: list[_SubstitutionType],
    ctx: LaunchContext,
) -> str:
    """Resolve a pre-parsed list of :class:`Substitution` objects to a string.

    Each token's ``.perform(ctx)`` is called in order and the results
    are concatenated.
    """
    return "".join(t.perform(ctx) for t in tokens)


def resolve_substitutions(
    text: str,
    ctx: LaunchContext,
) -> str:
    """Resolve all substitutions in a string using the given context.

    Parses *text* via the Lark grammar into typed :class:`Substitution`
    objects, then calls ``.perform()`` on each to produce the resolved
    string.
    """
    from roscope.parsers.parse_substitution import parse_substitution as _lark_parse

    tokens = _lark_parse(text)
    return resolve_substitutions_from_tokens(tokens, ctx)


# ─── Value resolution helper ─────────────────────────────────────────────────


def resolve_value(value: Any, ctx: LaunchContext | None = None) -> str | None:
    """Resolve a value to a string using context.perform_substitution().

    Supports: None, str, list[Substitution], single Substitution, anything else.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if ctx is not None:
        return ctx.perform_substitution(value)
    return str(value)


# ─── Condition evaluation ─────────────────────────────────────────────────────


def _is_truthy(value: str) -> bool:
    """Check if a resolved condition value is truthy (ROS 2 convention).

    Only ``"true"``/``"1"`` are truthy, ``"false"``/``"0"`` are falsy.
    Raises ``ValueError`` on anything else, matching the official
    ``InvalidConditionExpressionError`` behavior.
    """
    v = value.strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    raise ValueError(f"invalid condition expression: '{value}' (expected true/1/false/0)")


def _evaluate_condition(
    condition: dict[str, str] | None,
    ctx: LaunchContext,
) -> bool:
    """Evaluate an if/unless condition dict.  Returns True if the element should execute."""
    if condition is None:
        return True
    kind = condition["kind"]
    expr = condition["expr"]
    resolved = resolve_substitutions(expr, ctx)
    try:
        truthy = _is_truthy(resolved)
    except ValueError as e:
        logger.error("%s", e)
        return False
    if kind == "If":
        return truthy
    # Unless
    return not truthy


# ─── Namespace helpers ────────────────────────────────────────────────────────


def _ros2_namespace_join(base: str | None, next_ns: str | None) -> str | None:
    """Join two ROS 2 namespace components using official semantics.

    Delegates to ``make_namespace_absolute(prefix_namespace(base, next_ns))``.
    """
    from roscope.entities.utilities.namespace_utils import (
        make_namespace_absolute,
        prefix_namespace,
    )

    return make_namespace_absolute(prefix_namespace(base, next_ns))


def _effective_namespace(
    stack: list[str],
    explicit_ns: str | None = None,
) -> str | None:
    """Compute effective namespace from stack + optional node-level namespace."""
    current: str | None = None
    for component in stack:
        current = _ros2_namespace_join(current, component)
    if explicit_ns:
        current = _ros2_namespace_join(current, explicit_ns)
    return current


# ─── XML utilities ────────────────────────────────────────────────────────────


def sanitize_xml_comment(text: str) -> str:
    """Escape *text* so it is valid inside an XML comment.

    The XML spec forbids ``--`` inside comments and a trailing ``-``.
    ``ET.Comment`` does not escape automatically, so callers must sanitize.
    ``--`` is replaced with ``&#45;&#45;`` (both dashes as character references).
    A trailing ``-`` is replaced with ``&#45;``.
    """
    text = text.replace("--", "&#45;&#45;")
    if text.endswith("-"):
        text = text[:-1] + "&#45;"
    return text
