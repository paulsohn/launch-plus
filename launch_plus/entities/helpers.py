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

import launch_plus.resolver as _R
from launch_plus.entities.state import LaunchContext as _SubstitutionContext

logger = logging.getLogger("launch_plus")

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
    if value is None or isinstance(value, str):
        return False
    if hasattr(value, "perform"):
        return True
    if isinstance(value, (list, tuple)):
        return any(hasattr(item, "perform") for item in value)
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


def _current_source_key(state) -> str:
    """Return the source key for the current file being resolved.

    Uses the last entry of state.include_chain, or state.root_source_key for root-level.
    Format: "pkg://share_path".
    """
    if state.include_chain:
        pkg, path = state.include_chain[-1]
        return f"{pkg}://{path}" if pkg else path
    return str(state.root_source_key)


def _portable_display(sub) -> str:
    """Get the portable display string for a substitution without triggering resolution.

    Unlike ``str(sub)`` which may call ``_resolve_pkg_share()`` in non-preview
    mode, this always returns the portable form (e.g. ``$(find-pkg-share pkg)``).
    Used for recording unresolved declared arg defaults in --show-args metadata.
    """
    # Lazy imports to avoid circular dependencies
    from launch_plus.entities.substitutions.find_pkg_share import _TrackedFindPackageShare
    from launch_plus.entities.substitutions.path_join import _TrackedPathJoinSubstitution

    if isinstance(sub, _TrackedFindPackageShare):
        pkg, _ = sub._resolve_name(None)
        return f"$(find-pkg-share {pkg})"
    if isinstance(sub, _TrackedPathJoinSubstitution):
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
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "perform"):
        try:
            res = value.perform(context)
            return str(res) if res is not None else None
        except Exception:
            return str(value)
    return str(value)


def _resolve_substitution(sub: object, context: Any) -> str | None:
    """Resolve a substitution, list-of-substitutions, or plain string to str."""
    value, _fallback = _resolve_substitution_ex(sub, context)
    return value


def _resolve_substitution_ex(sub: object, context: Any) -> tuple[str | None, bool]:
    """Resolve a substitution and report whether the result is a fallback.

    Returns ``(resolved_str_or_None, is_fallback)`` where *is_fallback* is
    ``True`` when ``perform()`` returned ``None`` or raised and the display
    name was used instead.  Callers that need to distinguish "really resolved"
    from "fell back to variable name" (e.g. package tracking) should use this.
    """
    if sub is None:
        return None, False
    if isinstance(sub, str):
        # Plain strings from Python launch code are already resolved (they come from
        # Python expressions, not unresolved XML/YAML text).  Do NOT call
        # _resolve_ros_substitutions here — that would eagerly expand portable
        # $(find-pkg-share ...) paths back to machine-specific filesystem paths.
        return sub, False
    if isinstance(sub, (list, tuple)):
        parts = []
        any_fallback = False
        for s in sub:
            if hasattr(s, "perform"):
                if context is not None:
                    try:
                        result = s.perform(context)
                        if result is not None:
                            parts.append(str(result))
                        else:
                            parts.append(str(s))
                            any_fallback = True
                    except Exception:
                        parts.append(str(s))
                        any_fallback = True
                else:
                    parts.append(str(s))
                    any_fallback = True
            else:
                parts.append(str(s))
        return "".join(parts), any_fallback
    if context is not None and hasattr(sub, "perform"):
        try:
            result = sub.perform(context)
            if result is None:
                return str(sub), True  # unresolved — use display name
            return str(result), False
        except Exception:
            return str(sub), True
    return str(sub), True


def _track_package(state, pkg):
    if not pkg:
        return
    # Skip substitution objects (e.g. LaunchConfiguration) — the variable name
    # is NOT a real package.  The resolved value will be tracked later in
    # _resolve_node_details / _resolve_composable_plugins.
    if _is_substitution(pkg):
        return
    pkg = str(pkg)
    if pkg and not pkg.startswith("$(") and pkg not in state.tracked["packages"]:
        state.tracked["packages"].append(pkg)


def _track_node(state, node_dict: dict) -> int:
    """Append a node dict to state.tracked["nodes"] with source info from state.include_chain.

    Returns the index of the appended node.
    """
    if state.include_chain:
        node_dict["include_chain"] = list(state.include_chain)
    idx = len(state.tracked["nodes"])
    state.tracked["nodes"].append(node_dict)
    return idx


def _track_event_handler(state, eh_dict: dict) -> int:
    """Track an event handler as a node entry so it appears in encounter order.

    Wraps the event handler dict in a node-shaped dict with ``kind: "event_handler"``
    and uses ``_track_node()`` to get proper ``include_chain`` and ordering.
    """
    node_dict = {
        "package": "",
        "executable": "",
        "name": "",
        "namespace_stack": eh_dict.get("namespace_stack", []),
        "explicit_namespace": eh_dict.get("explicit_namespace"),
        "parameters": {},
        "param_files": [],
        "remappings": [],
        "env": {},
        "kind": "event_handler",
        "plugins": [],
        "target": eh_dict.get("target"),
        # Event-handler-specific fields
        "handler_kind": eh_dict.get("handler_kind", ""),
        "target_node": eh_dict.get("target_node"),
        "start_state": eh_dict.get("start_state"),
        "goal_state": eh_dict.get("goal_state"),
        "eh_actions": eh_dict.get("actions", []),
    }
    return _track_node(state, node_dict)


def _track_include(state, path, *, ros_namespace=None):
    if not path:
        return
    path = str(path)
    if path not in state.tracked["includes"]:
        state.tracked["includes"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {
            "package": dep[0],
            "share_path": dep[1],
            "path": path,
            "ros_namespace": ros_namespace,
        }
        # Don't deduplicate: the same file may be included multiple times under
        # different <push-ros-namespace> contexts, and each entry carries a distinct
        # namespace_stack needed for correct namespace propagation.
        entry["include_args"] = {}
        state.tracked["include_deps"].append(entry)
    # Return the index of the last entry with this path so callers can
    # attach include_args to the correct entry.
    for i in range(len(state.tracked["include_deps"]) - 1, -1, -1):
        if state.tracked["include_deps"][i].get("path") == path:
            return i
    return -1


def _track_param_file(state, path):
    if not path:
        return
    path = str(path)
    if path not in state.tracked["param_files"]:
        state.tracked["param_files"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {"package": dep[0], "share_path": dep[1]}
        if entry not in state.tracked["param_file_deps"]:
            state.tracked["param_file_deps"].append(entry)


def _record_declared_arg(state, name: str, default: str, *, flat: bool = True) -> None:
    """Record a declared arg in the per-file dict, and optionally the flat list.

    The flat list is used for apply_arg_defaults (first-declaration wins).
    The per-file dict is used by --show-args to render arg comments per included file.
    """
    if flat:
        state.tracked["declared_args"].append({"name": name, "default": default})
    key = _current_source_key(state)
    if key:
        by_file = state.tracked["declared_args_by_file"]
        if key not in by_file:
            by_file[key] = []
        by_file[key].append({"name": name, "default": default})


def _track_node_from_action(state, package, executable, name=None):
    """Track a node from an unpatched ROS 2 action (e.g. from OpaqueFunction return)."""
    # Pass raw package to _track_package BEFORE stringifying — _track_package
    # has an _is_substitution guard that filters out substitution objects.
    _track_package(state, package)
    package = str(package) if package else ""
    executable = str(executable) if executable else ""
    name = str(name) if name else ""
    return _track_node(
        state,
        {
            "package": package,
            "executable": executable,
            "name": name,
            "namespace_stack": [],
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "node",
            "plugins": [],
            "target": None,
        },
    )


# ─── Substitution Engine (for XML/YAML resolution) ───────────────────────────
#
# Parses and resolves ROS 2 substitution syntax: $(arg x), $(env Y default),
# $(find-pkg-share pkg), $(var x), $(dirname), $(eval expr), $(command ...).
# Used by the XML/YAML element walker — Python launch files use the
# existing .perform() mechanism instead.


def resolve_substitutions_from_tokens(
    tokens: list[_SubstitutionType],
    ctx: _SubstitutionContext,
) -> str:
    """Resolve a pre-parsed list of :class:`Substitution` objects to a string.

    Each token's ``.perform(ctx)`` is called in order and the results
    are concatenated.
    """
    return "".join(t.perform(ctx) for t in tokens)


def resolve_substitutions(
    text: str,
    ctx: _SubstitutionContext,
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


def resolve_value(value: Any, ctx: _SubstitutionContext | None = None) -> str | None:
    """Resolve a value to a string, handling all input types uniformly.

    Supports:
    - ``None`` → ``None``
    - ``str`` → returned as-is
    - ``list[Substitution]`` (from XML ``parse_substitution()``) → resolved via tokens
    - object with ``.perform()`` (Python shim substitution) → ``sub.perform(ctx)``
    - anything else → ``str(value)``
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # list[Substitution] from XML parse, or mixed list from Python shim
        if not value:
            return ""
        if ctx is not None and all(hasattr(t, "perform") for t in value):
            return resolve_substitutions_from_tokens(value, ctx)
        # Mixed list: resolve each element individually
        parts = []
        for t in value:
            if hasattr(t, "perform") and ctx is not None:
                try:
                    result = t.perform(ctx)
                    parts.append(str(result) if result is not None else str(t))
                except Exception:
                    parts.append(str(t))
            else:
                parts.append(str(t))
        return "".join(parts)
    if hasattr(value, "perform"):
        # Single substitution object (Python shim path)
        try:
            result = value.perform(ctx)
            return str(result) if result is not None else None
        except Exception:
            return str(value)
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
    ctx: _SubstitutionContext,
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


def _ros2_namespace_join(base: str | None, next_ns: str) -> str | None:
    """Join two ROS 2 namespace components."""
    next_ns = next_ns.rstrip("/")
    if not next_ns:
        return base
    if next_ns.startswith("/"):
        # Absolute — resets
        return next_ns
    if not base or base in ("", "/"):
        return f"/{next_ns}"
    return f"{base.rstrip('/')}/{next_ns}"


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
    ctx: _SubstitutionContext | None = None,
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
            if _R._ensure_fetched(state, pkg):
                pkg_share = state.package_shares.get(pkg)
            if not pkg_share:
                try:
                    pkg_share = _R._resolve_pkg_share(state, pkg)
                except Exception:
                    logger.error("param file not found: '%s' (package not available)", path)
                    return None
        real_path = os.path.join(pkg_share, rest)
    if not os.path.isfile(real_path):
        # Package share was known but file missing — try full fetch.
        if parsed and _R._ensure_fetched(state, parsed[0]):
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
