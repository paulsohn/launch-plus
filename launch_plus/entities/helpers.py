"""Pure helper functions extracted from resolver.py.

These functions track packages, nodes, includes, parameters, and event
handlers in :class:`~launch_plus.entities.state.ResolverState`.  They are
intentionally stateless with respect to module-level variables — every
function receives the state object explicitly.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

import yaml

from launch_plus.entities.state import LaunchContext

logger = logging.getLogger("launch_plus")


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
    from launch_plus.entities.substitution import Substitution as _SubstitutionType

# ─── Portable path support ────────────────────────────────────────────────────

_PORTABLE_PATH_RE = re.compile(r"^\$\(find-pkg-share ([^)]+)\)(.*)")


def _parse_portable_path(path_str: str):
    """Extract ``(pkg_name, rest)`` from a portable path string, or return ``None``.

    ``rest`` is the portion after the closing ``)`` with any leading separator stripped.
    For example::

        "$(find-pkg-share my_pkg)/config/file.yaml"  →  ("my_pkg", "config/file.yaml")
        "$(find-pkg-share my_pkg)"                   →  ("my_pkg", "")
        "/absolute/path"                             →  None
    """
    m = _PORTABLE_PATH_RE.match(path_str)
    if not m:
        return None
    pkg = m.group(1).strip()
    rest = m.group(2).lstrip("/").lstrip(os.sep)
    return pkg, rest


def _is_substitution(value):
    """Return True if *value* is a launch substitution (not yet resolved to a string).

    Handles both single substitution objects (with a ``perform`` method) and
    list-of-substitutions (``SomeSubstitutionsType`` in ROS 2), where any
    element may be a substitution object.
    """
    from launch_plus.entities.substitution import Substitution

    if value is None or isinstance(value, str):
        return False
    if isinstance(value, Substitution):
        return True
    if isinstance(value, (list, tuple)):
        return any(isinstance(item, Substitution) for item in value)
    return False


def _extract_pkg_and_share_path(path_str: str):
    """Extract (package, share_path) from a portable or AMENT install path.

    Handles two forms:
      - Portable: ``$(find-pkg-share pkg)/launch/foo.py`` → ``("pkg", "launch/foo.py")``
      - AMENT:    ``/opt/.../share/pkg/launch/foo.py``    → ``("pkg", "launch/foo.py")``

    Returns ``None`` if neither form matches.
    """
    parsed = _parse_portable_path(path_str)
    if parsed:
        return parsed
    # AMENT install path: .../share/<package>/<rest>
    idx = path_str.find("/share/")
    if idx >= 0 and "$(" not in path_str:
        after_share = path_str[idx + 7 :]  # skip "/share/"
        slash = after_share.find("/")
        if slash > 0:
            pkg = after_share[:slash]
            rest = after_share[slash + 1 :]
            if rest:
                return pkg, rest
    return None


def _portable_display(sub) -> str:
    """Get the portable display string for a substitution without triggering resolution.

    Unlike ``str(sub)`` which may call ``_resolve_pkg_share()`` in non-preview
    mode, this always returns the portable form (e.g. ``$(find-pkg-share pkg)``).
    Used for recording unresolved declared arg defaults in --show-args metadata.
    """
    # Lazy imports to avoid circular dependencies
    from launch_plus.entities.substitutions.find_pkg_share import FindPackageShare
    from launch_plus.entities.substitutions.path_join import PathJoinSubstitution

    if isinstance(sub, FindPackageShare):
        pkg, _ = sub._resolve_name(None)
        return f"$(find-pkg-share {pkg})"
    if isinstance(sub, PathJoinSubstitution):
        return "/".join(_portable_display(s) for s in sub._subs)
    return str(sub)


def _to_str(value: object, context: Any = None) -> str | None:
    """Coerce a str, substitution object, or None to ``str | None``.

    - ``None`` → ``None`` (caller decides how to handle missing values)
    - ``str``  → returned as-is
    - object with ``.perform()`` → call it; ``None`` result stays ``None``,
      non-``None`` result is coerced to ``str``; on exception → ``str(value)``
    - anything else → ``str(value)``

    Every non-``None`` return value is guaranteed to be ``str``.
    """
    from launch_plus.entities.substitution import Substitution

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
# Parses and resolves ROS 2 substitution syntax: $(arg x), $(env Y default),
# $(find-pkg-share pkg), $(var x), $(dirname), $(eval expr), $(command ...).
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
    from launch_plus.parsers.parse_substitution import parse_substitution as _lark_parse

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
        result = ctx.perform_substitution(value)
        return result if result else None
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
    from launch_plus.entities.utilities.namespace_utils import (
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


# ─── Parameter YAML expansion ────────────────────────────────────────────────


def _expand_ros_params_yaml(content: str) -> list[tuple[str, str]]:
    """Parse ROS 2 parameter YAML and flatten into (key, value) pairs.

    Supports all standard ROS 2 layouts:
      - bare ``ros__parameters: ...``
      - ``/**:\\n  ros__parameters: ...`` (Autoware wildcard convention)
      - ``/ns:\\n  node_name:\\n    ros__parameters: ...`` (general ROS 2)
    """
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    _collect_ros_params(data, 0, out)
    return out


def _collect_ros_params(value: object, depth: int, out: list[tuple[str, str]]) -> None:
    if depth > 3 or not isinstance(value, dict):
        return
    if "ros__parameters" in value:
        _flatten_yaml_value(value["ros__parameters"], "", out)
    else:
        for child in value.values():
            if isinstance(child, dict):
                _collect_ros_params(child, depth + 1, out)


def _flatten_yaml_value(value: object, prefix: str, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            full_key = f"{prefix}.{k}" if prefix else str(k)
            _flatten_yaml_value(v, full_key, out)
    elif isinstance(value, list):
        items = ", ".join(_yaml_value_to_str(v) for v in value)
        out.append((prefix, f"[{items}]"))
    else:
        out.append((prefix, _yaml_value_to_str(value)))


def _yaml_value_to_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, list):
        items = ", ".join(_yaml_value_to_str(x) for x in v)
        return f"[{items}]"
    return str(v)


def _read_and_expand_param_file(
    path: str,
    ctx: LaunchContext | None = None,
    *,
    state=None,
) -> list[tuple[str, str]] | None:
    """Read a param file and expand ros__parameters. Returns None on failure."""
    if state is None and ctx is not None:
        state = ctx._state
    real_path = path
    parsed = _parse_portable_path(path)
    if parsed:
        pkg, rest = parsed
        pkg_share = state.package_shares.get(pkg)
        if not pkg_share:
            # Try fetching the package if it's in the lockfile.
            if state._ensure_fetched(pkg):
                pkg_share = state.package_shares.get(pkg)
            if not pkg_share:
                try:
                    pkg_share = state.resolve_pkg_share(pkg)
                except Exception:
                    logger.error("param file not found: '%s' (package not available)", path)
                    return None
        real_path = os.path.join(pkg_share, rest)
    if not os.path.isfile(real_path):
        # Package share was known but file missing — try full fetch.
        if parsed and state._ensure_fetched(parsed[0]):
            pkg_share = state.package_shares.get(parsed[0])
            if pkg_share:
                real_path = os.path.join(pkg_share, parsed[1])
        if not os.path.isfile(real_path):
            logger.error("param file not found: '%s' (resolved from '%s')", real_path, path)
            return None
    try:
        with open(real_path) as f:
            content = f.read()
        pairs = _expand_ros_params_yaml(content)
        if pairs and ctx is not None:
            resolved_pairs: list[tuple[str, str]] = []
            for key, val in pairs:
                try:
                    resolved_val = resolve_substitutions(val, ctx)
                except Exception:
                    resolved_val = val
                resolved_pairs.append((key, resolved_val))
            return resolved_pairs
        return pairs
    except Exception as e:
        logger.error("--inline-params: failed to read '%s': %s", path, e)
        return None
