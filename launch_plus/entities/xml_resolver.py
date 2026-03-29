"""XML/YAML resolution engine.

Contains the substitution engine, XML/YAML element walker, and the
:class:`_ActionParser` facade that provides resolver services to action
handlers.  Python launch files use the import-patching machinery in
``resolver.py`` instead.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import yaml

import launch_plus.resolver as _R
from launch_plus.entities.expose import action_parse_methods
from launch_plus.entities.state import _error, _state, _warn
from launch_plus.parsers.entity import Entity
from launch_plus.parsers.xml_parser import parse_xml_launch as _parse_xml_launch_entity
from launch_plus.parsers.yaml_parser import parse_yaml_launch as _parse_yaml_launch_entity

if TYPE_CHECKING:
    from launch_plus.entities.substitution import Substitution as _SubstitutionType


# ─── Substitution Engine (for XML/YAML resolution) ───────────────────────────
#
# Parses and resolves ROS 2 substitution syntax: $(arg x), $(env Y default),
# $(find-pkg-share pkg), $(var x), $(dirname), $(eval expr), $(command ...).
# Used by the XML/YAML element walker — Python launch files use the
# existing .perform() mechanism instead.


class _SubstitutionContext:
    """Context for resolving substitutions in XML/YAML launch files.

    Carries a ``_state`` reference so that ``execute()`` methods receiving
    this as their context can access resolver state uniformly.
    """

    __slots__ = (
        "_state",
        "args",
        "vars",
        "env",
        "launch_file_dir",
        "preview_mode",
    )

    def __init__(self) -> None:
        self._state = _state
        self.args: dict[str, str] = {}
        self.vars: dict[str, str] = {}
        self.env: dict[str, str] = _state.env  # share with ResolverState
        self.launch_file_dir: str | None = None
        self.preview_mode: bool = False


def resolve_substitutions_from_tokens(
    tokens: list[_SubstitutionType],
    ctx: _SubstitutionContext,
    *,
    _depth: int = 0,
) -> str:
    """Resolve a pre-parsed list of :class:`Substitution` objects to a string.

    Each token's ``.perform(ctx)`` is called in order and the results
    are concatenated.  This is the low-level entry point used by
    individual substitution implementations when they need to recursively
    resolve nested tokens.
    """
    if _depth > 50:
        _error("substitution recursion limit exceeded")
        return "".join(t.serialize() for t in tokens)
    return "".join(t.perform(ctx, _depth=_depth) for t in tokens)


def resolve_substitutions(
    text: str,
    ctx: _SubstitutionContext,
    _depth: int = 0,
) -> str:
    """Resolve all substitutions in a string using the given context.

    Parses *text* via the Lark grammar into typed :class:`Substitution`
    objects, then calls ``.perform()`` on each to produce the resolved
    string.
    """
    if _depth > 50:
        _error(f"substitution recursion limit exceeded: {text[:100]}")
        return text
    from launch_plus.parsers.parse_substitution import parse_substitution as _lark_parse

    tokens = _lark_parse(text)
    return resolve_substitutions_from_tokens(tokens, ctx, _depth=_depth)


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
        # list[Substitution] from XML parse
        if not value:
            return ""
        if ctx is not None:
            return resolve_substitutions_from_tokens(value, ctx)
        return "".join(t.serialize() for t in value)
    if hasattr(value, "perform"):
        # Single substitution object (Python shim path)
        try:
            result = value.perform(ctx)
            return str(result) if result is not None else None
        except Exception:
            return str(value)
    return str(value)


# ─── XML/YAML AST Walker ────────────────────────────────────────────────────
#
# Walks the element list produced by parse_xml_launch / parse_yaml_launch,
# resolves substitutions, evaluates conditions, and populates _state.tracked.
# This is the XML/YAML counterpart of the Python _walk_actions mechanism.


def _is_truthy(value: str) -> bool:
    """Check if a resolved condition value is truthy (ROS 2 convention)."""
    return value.strip().lower() in ("true", "1", "yes", "on")


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
    truthy = _is_truthy(resolved)
    if kind == "If":
        return truthy
    # Unless
    return not truthy


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
) -> list[tuple[str, str]] | None:
    """Read a param file and expand ros__parameters. Returns None on failure."""
    real_path = path
    parsed = _R._parse_portable_path(path)
    if parsed:
        pkg, rest = parsed
        pkg_share = _state.package_shares.get(pkg)
        if not pkg_share:
            # Try fetching the package if it's in the lockfile.
            if _R._ensure_fetched(_R._state, pkg):
                pkg_share = _state.package_shares.get(pkg)
            if not pkg_share:
                try:
                    pkg_share = _R._resolve_pkg_share(_R._state, pkg)
                except Exception:
                    _error(f"param file not found: '{path}' (package not available)")
                    return None
        real_path = os.path.join(pkg_share, rest)
    if not os.path.isfile(real_path):
        # Package share was known but file missing — try full fetch.
        if parsed and _R._ensure_fetched(_R._state, parsed[0]):
            pkg_share = _state.package_shares.get(parsed[0])
            if pkg_share:
                real_path = os.path.join(pkg_share, parsed[1])
        if not os.path.isfile(real_path):
            _error(f"param file not found: '{real_path}' (resolved from '{path}')")
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
        _error(f"--inline-params: failed to read '{path}': {e}")
        return None


def resolve_xml_elements(
    elements: list[Entity],
    ctx: _SubstitutionContext,
    *,
    include_stack: list[str] | None = None,
) -> None:
    """Walk parsed XML/YAML elements, resolve substitutions, populate _state.tracked.

    This is the XML/YAML counterpart of the Python ``_walk_actions`` mechanism.
    All output goes into the module-level ``_state.tracked`` dict, ``_state.namespace_stack``,
    and ``_state.env``.

    Dispatches each element through the action registry via :func:`_resolve_element`.
    """
    if include_stack is None:
        include_stack = []
    for elem in elements:
        _resolve_element(elem, ctx, include_stack)


def _resolve_element(
    elem: Entity,
    ctx: _SubstitutionContext,
    include_stack: list[str],
) -> None:
    """Resolve a single parsed element via the action registry.

    The registered parse method may return an action instance (new style)
    or None (legacy — side effects already executed in parse).  When an
    action is returned, ``execute()`` is called to perform side effects.
    """
    tag = elem.type_name
    if tag in action_parse_methods:
        parser = _ActionParser(ctx, include_stack)
        action = action_parse_methods[tag](elem, parser)
        if action is not None and hasattr(action, "execute"):
            action.execute(ctx)
        return
    _warn(f"unknown element: <{tag}>")


# ── ActionParser — resolver services for action handlers ─────────────────────


class _ActionParser:
    """Stateless parsing helper for action ``parse()`` classmethods.

    Provides substitution token parsing and condition evaluation.
    Does NOT resolve substitutions or access mutable state — ``parse()``
    methods use this to build unresolved action instances.
    """

    __slots__ = ("ctx", "include_stack")

    def __init__(self, ctx: _SubstitutionContext, include_stack: list[str]) -> None:
        self.ctx = ctx
        self.include_stack = include_stack

    def parse_substitution(self, text: str) -> list:
        """Parse ``$(...)`` substitutions in *text* into token objects.

        Returns a list of :class:`Substitution` objects that can be resolved
        later via ``resolve_substitutions_from_tokens()``.
        """
        from launch_plus.parsers.parse_substitution import parse_substitution as _lark_parse

        return _lark_parse(text)

    def evaluate_condition(self, entity: Entity) -> bool:
        """Evaluate if=/unless= on *entity*.  Returns True → element should execute."""
        if_val = entity.get_attr("if", optional=True)
        unless_val = entity.get_attr("unless", optional=True)
        cond: dict[str, str] | None = None
        if if_val is not None:
            cond = {"kind": "If", "expr": if_val}
        elif unless_val is not None:
            cond = {"kind": "Unless", "expr": unless_val}
        return _evaluate_condition(cond, self.ctx)

    # ── Unresolved extraction from Entity children ───────────────────

    def parse_params(self, entity: Entity) -> list:
        """Extract <param> children as unresolved token structures.

        Returns a list of dicts, each either:
        - ``{"name": tokens, "value": tokens}`` for inline params
        - ``{"from": tokens}`` for param file references
        """
        items = entity.get_attr("param", data_type=list, optional=True)
        if not items:
            return []
        result = []
        for p in items:
            name = p.get_attr("name", optional=True)
            value = p.get_attr("value", optional=True)
            from_file = p.get_attr("from", optional=True)
            if from_file:
                result.append({"from": self.parse_substitution(from_file)})
            elif name:
                result.append(
                    {
                        "name": self.parse_substitution(name),
                        "value": self.parse_substitution(value or ""),
                    }
                )
        return result

    def parse_remaps(self, entity: Entity) -> list:
        """Extract <remap> children as unresolved token pairs."""
        items = entity.get_attr("remap", data_type=list, optional=True)
        if not items:
            return []
        return [
            (
                self.parse_substitution(r.get_attr("from", optional=True) or ""),
                self.parse_substitution(r.get_attr("to", optional=True) or ""),
            )
            for r in items
        ]

    def parse_envs(self, entity: Entity) -> list:
        """Extract <env> children as unresolved token pairs."""
        items = entity.get_attr("env", data_type=list, optional=True)
        if not items:
            return []
        return [
            (
                self.parse_substitution(e.get_attr("name", optional=True) or ""),
                self.parse_substitution(e.get_attr("value", optional=True) or ""),
            )
            for e in items
        ]

    def parse_composable_plugins(self, entity: Entity) -> list:
        """Extract <composable_node> children as unresolved plugin dicts."""
        items = entity.get_attr("composable_node", data_type=list, optional=True)
        if not items:
            return []
        plugins = []
        for cn in items:
            # Condition must be evaluated at parse time (determines structure)
            cond_if = cn.get_attr("if", optional=True)
            cond_unless = cn.get_attr("unless", optional=True)
            cond: dict[str, str] | None = None
            if cond_if is not None:
                cond = {"kind": "If", "expr": cond_if}
            elif cond_unless is not None:
                cond = {"kind": "Unless", "expr": cond_unless}
            if not _evaluate_condition(cond, self.ctx):
                continue
            plugins.append(
                {
                    "package": self.parse_substitution(cn.get_attr("pkg", optional=True) or ""),
                    "plugin": self.parse_substitution(cn.get_attr("plugin", optional=True) or ""),
                    "name": cn.get_attr("name", optional=True),
                    "params": self.parse_params(cn),
                    "remaps": self.parse_remaps(cn),
                }
            )
        return plugins


# ── Include file resolution (execution-time) ─────────────────────────────────


def resolve_included_file(
    ctx: _SubstitutionContext,
    include_stack: list[str],
    real_path: str,
    file_path: str,
    child_ctx_args: dict[str, str],
) -> None:
    """Parse an included launch file and resolve it recursively."""
    inc_dep = _R._extract_pkg_and_share_path(file_path)
    if inc_dep:
        _state.include_chain.append(list(inc_dep))
    else:
        _state.include_chain.append(["", file_path])
    new_stack = include_stack + [file_path]
    try:
        if real_path.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
            with open(real_path) as f:
                content = f.read()
            child_entities: list[Entity]
            if real_path.endswith((".yaml", ".yml")):
                child_entities = list(_parse_yaml_launch_entity(content, real_path))
            else:
                child_entities = list(_parse_xml_launch_entity(content, real_path))
            child_ctx = _SubstitutionContext()
            if _state.global_arg_cascade:
                child_ctx.args = {**ctx.args, **child_ctx_args}
                child_ctx.vars = {**ctx.vars, **child_ctx_args}
            else:
                child_ctx.args = dict(child_ctx_args)
                child_ctx.vars = dict(child_ctx_args)
            child_ctx.env = dict(ctx.env)
            child_ctx.launch_file_dir = os.path.dirname(real_path)
            child_ctx.preview_mode = ctx.preview_mode
            for child in child_entities:
                _resolve_element(child, child_ctx, new_stack)
            ctx.args.update(child_ctx.args)
            ctx.vars.update(child_ctx.vars)
        elif real_path.endswith((".launch.py", ".py")):
            parent_lc = _R._make_launch_context({**ctx.args, **ctx.vars})
            if _state.global_params:
                parent_lc._launch_configurations["global_params"] = list(_state.global_params)
            _R._inline_resolve_python_launch(_R._state, file_path, parent_lc, child_ctx_args)
            set_configs = _state.tracked["set_launch_configurations"]
            for k, v in parent_lc._launch_configurations.items():
                if (k in set_configs or k in child_ctx_args) and k != "global_params":
                    ctx.vars[k] = str(v) if not isinstance(v, str) else v
    finally:
        _state.include_chain.pop()
