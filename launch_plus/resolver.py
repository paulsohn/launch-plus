#!/usr/bin/env python3
"""
launch-plus resolver.

Resolves ROS 2 launch files (XML, YAML, and Python) by parsing their
structure, evaluating substitutions, and tracking nodes, includes, and
package dependencies.  Python launch files are handled via import-patching
hooks that intercept ``launch_ros`` and ``launch`` classes.

The main entry point is :func:`resolve_file`, which returns a
:class:`~launch_plus.types.ParsedLaunchFile`.
"""

import contextvars
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("launch_plus")

# Ensure substitution entity classes are registered before any parsing occurs.
import launch_plus.entities  # noqa: F401
from launch_plus.entities.state import (  # noqa: E402
    LaunchContext,
    ResolverState,
)

# ─── Scoped state via contextvars ────────────────────────────────────────────
#
# resolve_file() creates a fresh ResolverState and sets _current_state for the
# duration of resolution.  All code that needs state uses get_state().
# This is thread-safe and supports concurrent resolution.

_current_state: contextvars.ContextVar[ResolverState] = contextvars.ContextVar(
    "_current_state",
)
# Set a default state for test/REPL use before resolve_file() is called.
_current_state.set(ResolverState())


def get_state() -> ResolverState:
    """Return the active ResolverState for the current execution context."""
    return _current_state.get()


# ─── Capture real ament_index_python BEFORE installing the import patcher ────
# This allows FindPackageShare.perform() to return real installed paths when
# the package is actually present in the build/install tree.

_real_get_package_share_directory = None
try:
    from ament_index_python.packages import get_package_share_directory as _real_gps

    _real_get_package_share_directory = _real_gps
except Exception:
    pass

# ─── ROS prefix fallback ─────────────────────────────────────────────────────
# Use $ROS_DISTRO env var so this works across distro versions (jazzy, rolling, etc.)
_ROS_DISTRO = os.environ.get("ROS_DISTRO", "")
_ROS_DISTRO_PREFIX = f"/opt/ros/{_ROS_DISTRO}" if _ROS_DISTRO else ""


# ─── XML/YAML Launch File Parser ──────────────────────────────────────────────
#
# Parsing is delegated to Entity-based parsers in ``launch_plus.parsers``.
# Resolution is dispatched through the action registry via _resolve_element().

import xml.etree.ElementTree as ET  # noqa: F401 — still used by callers

from launch_plus.parsers.entity import Entity
from launch_plus.parsers.xml_parser import parse_xml_launch as _parse_xml_launch_entity
from launch_plus.parsers.yaml_parser import parse_yaml_launch as _parse_yaml_launch_entity


def parse_xml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse an XML launch file to a list of Entity objects."""
    return list(_parse_xml_launch_entity(content, file_path))


def parse_yaml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse a YAML launch file to a list of Entity objects."""
    return list(_parse_yaml_launch_entity(content, file_path))


# ─── XML/YAML resolution engine (extracted to entities/xml_resolver.py) ──────
# ── Action handler registration ───────────────────────────────────────────────
#
# Action handlers live in launch_plus.entities.actions.*.  Importing the
# package triggers @expose_action registration into action_parse_methods.
import launch_plus.entities.actions  # noqa: F401, E402

# ─── Internal imports (used by resolver logic) ───────────────────────────────
from launch_plus.entities.actions.arg import (  # noqa: E402
    DeclareLaunchArgument,
    _apply_declared_arg,
)
from launch_plus.entities.helpers import _extract_pkg_and_share_path  # noqa: E402
from launch_plus.entities.parsing import _ActionParser  # noqa: E402

# ─── LaunchContext factory ────────────────────────────────────────────────────


def _make_launch_context(args_dict):
    """Create a LaunchContext pre-populated with provided args."""
    ctx = LaunchContext()
    ctx._launch_configurations = dict(args_dict)
    return ctx


# ─── Inline Python include resolution ─────────────────────────────────────────


def _inline_resolve_python_launch(state, launch_file, parent_context, child_args) -> list:
    """Load a Python launch file and walk its actions in the parent context.

    This mirrors real ROS 2 behavior where ``IncludeLaunchDescription``
    synchronously executes the child, so ``SetLaunchConfiguration`` calls in
    the child mutate the shared ``LaunchContext``.
    """

    real_path = launch_file
    if not os.path.isfile(real_path):
        return []

    try:
        spec = importlib.util.spec_from_file_location(
            f"_inline_launch_{len(state.include_chain)}", real_path
        )
        if spec is None or spec.loader is None:
            logger.warning("cannot load included launch file: %s", real_path)
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.warning("failed to load included launch file %s: %s", real_path, e)
        return []

    if not hasattr(mod, "generate_launch_description"):
        return []

    saved_declared_arg_names = set(state.declared_arg_names)
    try:
        try:
            ld = mod.generate_launch_description()
        except Exception as e:
            logger.warning("generate_launch_description() failed in %s: %s", launch_file, e)
            return []

        entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

        for k, v in child_args.items():
            parent_context._launch_configurations[k] = v

        for entity in entities:
            if isinstance(entity, DeclareLaunchArgument):
                _apply_declared_arg(entity, parent_context)

        return _execute_actions(entities, parent_context)
    finally:
        state.declared_arg_names.clear()
        state.declared_arg_names.update(saved_declared_arg_names)


# ─── XML/YAML AST Walker ────────────────────────────────────────────────────
#
# Walks the element list produced by parse_xml_launch / parse_yaml_launch,
# resolves substitutions, evaluates conditions, and populates _state.tracked.
# This is the XML/YAML counterpart of the Python _walk_actions mechanism.


def resolve_xml_elements(
    elements: list,
    ctx,
    *,
    include_stack: list[str] | None = None,
) -> list:
    """Walk parsed XML/YAML elements, return resolved actions."""
    if include_stack is None:
        include_stack = []
    results: list = []
    for elem in elements:
        results.extend(_resolve_element(elem, ctx, include_stack))
    return results


def _resolve_element(
    elem,
    ctx,
    include_stack: list[str],
) -> list:
    """Parse and execute a single element. Returns resolved actions."""
    from launch_plus.entities.expose import action_parse_methods

    tag = elem.type_name
    if tag in action_parse_methods:
        parser = _ActionParser(ctx, include_stack)
        action = action_parse_methods[tag](elem, parser)
        if action is not None and hasattr(action, "execute"):
            return action.execute(ctx) or []
        return []
    logger.warning("unknown element: <%s>", tag)
    return []


def resolve_included_file(
    ctx,
    include_stack: list[str],
    real_path: str,
    file_path: str,
    child_ctx_args: dict[str, str],
) -> list:
    """Parse an included launch file and return resolved actions."""
    from launch_plus.entities.helpers import _extract_pkg_and_share_path
    from launch_plus.parsers.xml_parser import parse_xml_launch as _parse_xml_launch_entity
    from launch_plus.parsers.yaml_parser import parse_yaml_launch as _parse_yaml_launch_entity

    state = ctx._state
    inc_dep = _extract_pkg_and_share_path(file_path)
    if inc_dep:
        state.include_chain.append(list(inc_dep))
    else:
        state.include_chain.append(["", file_path])
    new_stack = include_stack + [file_path]
    for k, v in child_ctx_args.items():
        ctx._launch_configurations[k] = v
    saved_launch_file_dir = ctx.launch_file_dir
    ctx.launch_file_dir = os.path.dirname(real_path)
    results: list = []
    try:
        if real_path.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
            with open(real_path) as f:
                content = f.read()
            child_entities: list
            if real_path.endswith((".yaml", ".yml")):
                child_entities = list(_parse_yaml_launch_entity(content, real_path))
            else:
                child_entities = list(_parse_xml_launch_entity(content, real_path))
            results = resolve_xml_elements(child_entities, ctx, include_stack=new_stack)
        elif real_path.endswith((".launch.py", ".py")):
            results = _inline_resolve_python_launch(state, file_path, ctx, child_ctx_args)
    finally:
        ctx.launch_file_dir = saved_launch_file_dir
        state.include_chain.pop()
    return results


def _execute_actions(actions, context) -> list:
    """Execute a list of actions and collect resolved results."""
    from launch_plus.entities.action import Action

    results: list = []
    for action in actions or []:
        if action is None:
            continue
        if not isinstance(action, Action):
            logger.error("expected Action, got %s", type(action).__name__)
            continue
        children = action.execute(context)
        if children:
            results.extend(children)
    return results


# ─── Import system patcher (extracted to entities/import_patcher.py) ──────────
from launch_plus.entities.import_patcher import (  # noqa: E402, F401
    _PATCHED_MODULES,
    _PatchingFinder,
)


def resolve_file(
    launch_file: "Path",
    args: dict[str, str],
    package_shares: dict[str, str],
    workflow_options: Any = None,
    lockfile: Any = None,
    fetch_dir: "Path | None" = None,
    global_params: list | None = None,
    fetch_options: Any = None,
) -> Any:
    """Resolve a launch file and return a ParsedLaunchFile.

    Parameters
    ----------
    launch_file : Path
        Absolute path to the launch file.
    args : dict
        Launch arguments (``name:=value`` pairs).
    package_shares : dict
        Package name → share directory path.
    workflow_options : ResolveWorkflowOptions
        Workflow flags.
    lockfile : Lockfile
        Lockfile with package/repo info.
    fetch_dir : Path | None
        Directory for sparse-checkout.
    fetch_options : FetchOptions | None
        Options for inline git sparse-checkout.
    global_params : list | None
        Persisted global params from prior files.

    Returns
    -------
    ParsedLaunchFile
        The resolver result as a structured object (imported from types module).
    """
    state = ResolverState()
    token = _current_state.set(state)
    try:
        return _resolve_file_impl(
            state,
            launch_file,
            args,
            package_shares,
            workflow_options,
            lockfile,
            fetch_dir,
            global_params,
            fetch_options,
        )
    finally:
        _current_state.reset(token)


def _resolve_file_impl(
    state: ResolverState,
    launch_file: "Path",
    args: dict[str, str],
    package_shares: dict[str, str],
    workflow_options: Any = None,
    lockfile: Any = None,
    fetch_dir: "Path | None" = None,
    global_params: list | None = None,
    fetch_options: Any = None,
) -> Any:
    launch_file_str = str(launch_file)

    root_dep = _extract_pkg_and_share_path(launch_file_str)
    state.root_source_key = f"{root_dep[0]}://{root_dep[1]}" if root_dep else launch_file_str

    state.fetched_packages.clear()
    state.declared_arg_names.clear()
    state.include_chain.clear()
    state.package_shares = dict(package_shares)

    # Reset state.tracked
    state.tracked["packages"] = []
    state.tracked["includes"] = []
    state.tracked["declared_args"] = []
    state.tracked["declared_args_by_file"] = {}
    state.tracked["global_params"] = []
    state.tracked["include_args"] = {}
    state.tracked["param_files"] = []
    state.tracked["set_launch_configurations"] = {}
    state.tracked["include_deps"] = []
    state.tracked["param_file_deps"] = []

    # Workflow flags
    if workflow_options is not None:
        state.preview_mode = bool(getattr(workflow_options, "preview", True))
        state.inline_params = bool(getattr(workflow_options, "inline_params", False))
        state.rosdep_fallback = bool(getattr(workflow_options, "rosdep_fallback", False))
        state.apply_arg_defaults = bool(getattr(workflow_options, "apply_arg_defaults", False))
        state.show_empty_includes = bool(getattr(workflow_options, "show_empty_includes", False))
        state.show_args = bool(getattr(workflow_options, "show_args", False))
    else:
        state.preview_mode = True
        state.inline_params = False
        state.rosdep_fallback = False
        state.apply_arg_defaults = False
        state.show_empty_includes = False
        state.show_args = False

    # Build lockfile data from the Lockfile dataclass
    if lockfile is not None:
        lf_data = {}
        for pkg_name, pkg_lock in lockfile.packages.items():
            repo_lock = lockfile.repositories.get(pkg_lock.repo)
            if repo_lock is not None:
                lf_data[pkg_name] = {
                    "repo": pkg_lock.repo,
                    "path": pkg_lock.path,
                    "url": repo_lock.url,
                    "version": repo_lock.version,
                }
        state.lockfile_data = lf_data
    else:
        state.lockfile_data = {}

    state.fetch_dir = str(fetch_dir) if fetch_dir else ""
    state.fetch_options = fetch_options  # FetchOptions from CLI/orchestrator

    args_dict = dict(args)

    # Pre-populate global params
    persisted_global_params = list(global_params) if global_params else []

    # Install the import patcher
    sys.meta_path.insert(0, _PatchingFinder())
    for mod_name, builder in _PatchingFinder.PATCHED.items():
        if mod_name not in _PATCHED_MODULES:
            _PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = _PATCHED_MODULES[mod_name]

    # ── Resolve by file type ─────────────────────────────────────────────
    if launch_file_str.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
        try:
            with open(launch_file_str) as f:
                content = f.read()
        except Exception as e:
            logger.error("cannot read %s: %s", launch_file_str, e)
            return _tracked_to_parsed_launch_file(state.tracked), []

        if launch_file_str.endswith((".yaml", ".yml")):
            elements = parse_yaml_launch(content, launch_file_str)
        else:
            elements = parse_xml_launch(content, launch_file_str)
        subst_ctx = LaunchContext(state)
        subst_ctx._launch_configurations = dict(args_dict)
        subst_ctx.launch_file_dir = os.path.dirname(os.path.abspath(launch_file_str))
        subst_ctx.preview_mode = state.preview_mode
        resolved = resolve_xml_elements(elements, subst_ctx, include_stack=[launch_file_str])
        return _tracked_to_parsed_launch_file(state.tracked), resolved

    # ── Python launch files ──────────────────────────────────────────────
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file_str)
    if spec is None or spec.loader is None:
        logger.error("cannot load %s", launch_file_str)
        return _tracked_to_parsed_launch_file(state.tracked), []

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.error("Error loading launch file: %s", e)
        return _tracked_to_parsed_launch_file(state.tracked), []

    if not hasattr(mod, "generate_launch_description"):
        logger.error("No generate_launch_description() function found")
        return _tracked_to_parsed_launch_file(state.tracked), []

    # Inject persisted global params
    if "__global_params__" in args_dict:
        try:
            persisted_global_params = json.loads(args_dict.pop("__global_params__"))
        except Exception:
            pass

    ctx = _make_launch_context(args_dict)

    if persisted_global_params:
        gp_tuples = [(entry[0], entry[1]) for entry in persisted_global_params if len(entry) == 2]
        ctx._launch_configurations["global_params"] = list(gp_tuples)

    try:
        ld = mod.generate_launch_description()
    except Exception as e:
        logger.error("generate_launch_description() failed: %s", e)
        return _tracked_to_parsed_launch_file(state.tracked), []

    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, DeclareLaunchArgument):
            _apply_declared_arg(entity, ctx)

    resolved = _execute_actions(entities, ctx)

    return _tracked_to_parsed_launch_file(state.tracked), resolved


def _tracked_to_parsed_launch_file(tracked: dict[str, Any]) -> Any:
    """Build a ParsedLaunchFile from tracked state and resolved actions."""
    from pathlib import Path as _Path

    from launch_plus.types import (
        DependencyKind as _DependencyKind,
    )
    from launch_plus.types import (
        FileDependency as _FileDependency,
    )
    from launch_plus.types import (
        LaunchInclude as _LaunchInclude,
    )
    from launch_plus.types import (
        ParsedLaunchFile as _ParsedLaunchFile,
    )

    # ── Include deps ──────────────────────────────────────────────────
    launch_includes: list[_LaunchInclude] = []
    include_args_map = tracked.get("include_args", {})
    for dep in tracked.get("include_deps", []):
        dep_include_args = dep.get("include_args", {})
        if not dep_include_args:
            dep_include_args = include_args_map.get(dep.get("path", ""), {})
        launch_includes.append(
            _LaunchInclude(
                package=dep["package"],
                share_path=_Path(dep["share_path"]),
                explicit_args=dep_include_args,
                namespace_stack=[dep["ros_namespace"]] if dep.get("ros_namespace") else [],
            )
        )

    # ── Declared args ─────────────────────────────────────────────────
    declared_arg_defaults = {a["name"]: a["default"] for a in tracked.get("declared_args", [])}

    # ── Param file deps ───────────────────────────────────────────────
    param_file_deps = [
        _FileDependency(
            package=dep["package"],
            share_path=_Path(dep["share_path"]),
            kind=_DependencyKind.PARAM,
        )
        for dep in tracked.get("param_file_deps", [])
    ]

    # ── Per-file declared args ────────────────────────────────────────
    declared_args_by_file: dict[tuple[str, _Path], dict[str, str]] = {}
    for key_str, args_list in tracked.get("declared_args_by_file", {}).items():
        if "://" in key_str:
            idx = key_str.index("://")
            pkg = key_str[:idx]
            sp = _Path(key_str[idx + 3 :])
        else:
            pkg = ""
            sp = _Path(key_str)
        declared_args_by_file[(pkg, sp)] = {a["name"]: a["default"] for a in args_list}

    return _ParsedLaunchFile(
        packages=list(tracked.get("packages", [])),
        launch_includes=launch_includes,
        param_files=param_file_deps,
        declared_arg_defaults=declared_arg_defaults,
        declared_args_by_file=declared_args_by_file,
        global_params=list(tracked.get("global_params", [])),
    )
