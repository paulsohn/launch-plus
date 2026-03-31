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
# ─── Param-file stubs for OpaqueFunction resilience ─────────────────────────
#
# OpaqueFunction bodies in Autoware typically call `open(param_file)` and
# `yaml.safe_load(f)` to read ROS 2 parameter YAML files before constructing
# their composable nodes.  In preview mode these paths are resolved to
# workspace-relative strings which may not exist (e.g. naming mismatches
# between the autoware_launch config and the package's own config directory).
#
# To recover from this edge case we temporarily patch `builtins.open` and
# `yaml.safe_load` during OpaqueFunction execution:
#   - open(): if the requested file is not found, emit a warning and return a
#     StringIO stub containing a minimal valid ROS 2 parameter YAML.
#   - yaml.safe_load(): wraps any `ros__parameters` dict in `_DefaultParamDict`
#     so that missing keys return `False` instead of raising `KeyError`.
#     Boolean-flag guards such as `if params["downsample_input_pointcloud"]:`
#     then evaluate to False, skipping optional branches while still letting
#     the function reach and register its core composable nodes.
#
# The patches are installed/removed atomically around `fn(context)` via
# `_call_opaque_with_stubs`.

import launch_plus.entities.actions  # noqa: F401, E402

# ─── Internal imports (used by resolver logic) ───────────────────────────────
from launch_plus.entities.action import Action  # noqa: E402
from launch_plus.entities.actions.arg import (  # noqa: E402
    DeclareLaunchArgument,
    _apply_declared_arg,
)
from launch_plus.entities.helpers import (  # noqa: E402
    _extract_pkg_and_share_path,
    _parse_portable_path,
    _ros2_namespace_join,
)
from launch_plus.entities.opaque_stubs import _call_opaque_with_stubs  # noqa: E402
from launch_plus.entities.parsing import _ActionParser  # noqa: E402

# ─── LaunchContext factory ────────────────────────────────────────────────────


def _make_launch_context(args_dict):
    """Create a LaunchContext pre-populated with provided args."""
    ctx = LaunchContext()
    ctx._launch_configurations = dict(args_dict)
    return ctx


# ─── Inline Python include resolution ─────────────────────────────────────────


def _inline_resolve_python_launch(state, launch_file, parent_context, child_args):
    """Load a Python launch file and walk its actions in the parent context.

    This mirrors real ROS 2 behavior where ``IncludeLaunchDescription``
    synchronously executes the child, so ``SetLaunchConfiguration`` calls in
    the child mutate the shared ``LaunchContext``.
    """

    # Resolve portable paths — $(find-pkg-share pkg)/rest → real filesystem path.
    real_path = launch_file
    parsed = _parse_portable_path(launch_file)
    if parsed:
        pkg, rest = parsed
        try:
            pkg_share = state.resolve_pkg_share(pkg)
        except Exception:
            return  # Package not available
        real_path = os.path.join(pkg_share, rest)

    if not os.path.isfile(real_path):
        return  # File not on disk

    try:
        spec = importlib.util.spec_from_file_location(
            f"_inline_launch_{state.walk_depth}", real_path
        )
        if spec is None or spec.loader is None:
            logger.warning("cannot load included launch file: %s", real_path)
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.warning("failed to load included launch file %s: %s", real_path, e)
        return

    if not hasattr(mod, "generate_launch_description"):
        return

    # Matching official IncludeLaunchDescription(scoped=False): child actions
    # mutate the parent_context directly.  All side-effects persist.
    saved_declared_arg_names = set(state.declared_arg_names)

    try:
        try:
            ld = mod.generate_launch_description()
        except Exception as e:
            logger.warning("generate_launch_description() failed in %s: %s", launch_file, e)
            return

        entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

        # Set child launch arguments (matching official SetLaunchConfiguration pattern).
        for k, v in child_args.items():
            parent_context._launch_configurations[k] = v

        # Pass 1: apply DeclareLaunchArgument defaults.
        for entity in entities:
            if isinstance(entity, DeclareLaunchArgument):
                _apply_declared_arg(entity, parent_context)

        # Pass 2: walk all actions in the shared context.
        _walk_actions(state, entities, parent_context)
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
) -> None:
    """Walk parsed XML/YAML elements, resolve substitutions, populate _state.tracked.

    This is the XML/YAML counterpart of the Python ``_walk_actions`` mechanism.
    All output goes into the module-level ``_state.tracked`` dict.

    Dispatches each element through the action registry via :func:`_resolve_element`.
    """
    if include_stack is None:
        include_stack = []
    for elem in elements:
        _resolve_element(elem, ctx, include_stack)


def _resolve_element(
    elem,
    ctx,
    include_stack: list[str],
) -> None:
    """Resolve a single parsed element via the action registry.

    The registered parse method may return an action instance (new style)
    or None (legacy — side effects already executed in parse).  When an
    action is returned, ``execute()`` is called to perform side effects.
    """
    from launch_plus.entities.expose import action_parse_methods

    tag = elem.type_name
    if tag in action_parse_methods:
        parser = _ActionParser(ctx, include_stack)
        action = action_parse_methods[tag](elem, parser)
        if action is not None and hasattr(action, "execute"):
            action.execute(ctx)
        return
    logger.warning("unknown element: <%s>", tag)


def resolve_included_file(
    ctx,
    include_stack: list[str],
    real_path: str,
    file_path: str,
    child_ctx_args: dict[str, str],
) -> None:
    """Parse an included launch file and resolve it recursively."""
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
    # Set child args in the SAME context (matching official IncludeLaunchDescription
    # scoped=False: child mutations persist in parent).
    for k, v in child_ctx_args.items():
        ctx._launch_configurations[k] = v
    saved_launch_file_dir = ctx.launch_file_dir
    ctx.launch_file_dir = os.path.dirname(real_path)
    try:
        if real_path.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
            with open(real_path) as f:
                content = f.read()
            child_entities: list
            if real_path.endswith((".yaml", ".yml")):
                child_entities = list(_parse_yaml_launch_entity(content, real_path))
            else:
                child_entities = list(_parse_xml_launch_entity(content, real_path))
            for child in child_entities:
                _resolve_element(child, ctx, new_stack)
        elif real_path.endswith((".launch.py", ".py")):
            _inline_resolve_python_launch(state, file_path, ctx, child_ctx_args)
    finally:
        ctx.launch_file_dir = saved_launch_file_dir
        state.include_chain.pop()


# ─── Action walker ────────────────────────────────────────────────────────────


def _walk_actions(state, actions, context):
    """Walk a list of actions, calling ``execute()`` on each.

    Tracked actions (subclasses of ``Action``) implement the polymorphic
    ``execute(context)`` method.  Untracked actions (real ROS 2 objects from
    OpaqueFunction returns) are handled by ``_walk_untracked_action()``.
    """
    if actions is None:
        return
    state.walk_depth += 1
    if state.walk_depth > 20:
        state.walk_depth -= 1
        logger.warning("Max include depth reached while walking Python launch description")
        return
    try:
        for action in actions:
            if action is None:
                continue
            try:
                if isinstance(action, Action):
                    children = action.execute(context)
                    if children:
                        _walk_actions(state, children, context)
                else:
                    _walk_untracked_action(state, action, context)
            except Exception as e:
                logger.error("Error walking action %s: %s", type(action).__name__, e)
    finally:
        state.walk_depth -= 1


def _walk_untracked_action(state, action, context):
    """Handle real (unpatched) ROS 2 action objects.

    These come from OpaqueFunction returns that produce real ``launch_ros``
    classes instead of our shimmed ``_Tracked*`` wrappers.
    """
    cls_name = type(action).__name__

    # OpaqueFunction: execute its function and walk the result
    if cls_name == "OpaqueFunction" or (
        hasattr(action, "function") and callable(getattr(action, "function", None))
    ):
        fn = getattr(action, "function", None)
        if fn and context:
            try:
                result = _call_opaque_with_stubs(state, fn, context)
                if result:
                    _walk_actions(state, result, context)
            except Exception as e:
                logger.error("OpaqueFunction failed: %s", e)
        return

    # Walk nested actions/entities from generic action objects
    if hasattr(action, "entities"):
        try:
            _walk_actions(state, action.entities, context)
        except Exception as e:
            logger.warning("failed to walk %s.entities: %s", cls_name, e)
    if hasattr(action, "_actions"):
        try:
            _walk_actions(state, action._actions, context)
        except Exception as e:
            logger.warning("failed to walk %s._actions: %s", cls_name, e)

    # Real Node/LifecycleNode from launch_ros (unpatched, e.g. from OpaqueFunction return)
    if cls_name in ("Node", "LifecycleNode") and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        state.track_node_from_action(pkg, exe)
    elif cls_name == "ComposableNodeContainer" and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        name = getattr(action, "_name", None)
        state.track_node_from_action(pkg, exe, name)
        descs = (
            getattr(
                action,
                "composable_node_descriptions",
                getattr(action, "_composable_node_descriptions", None),
            )
            or []
        )
        for desc in descs:
            state.track_node_from_action(
                getattr(desc, "package", None),
                getattr(desc, "plugin", None),
                getattr(desc, "node_name", getattr(desc, "name", None)),
            )
    elif cls_name == "LoadComposableNodes":
        descs = (
            getattr(
                action,
                "_composable_node_descriptions",
                getattr(action, "composable_node_descriptions", None),
            )
            or []
        )
        for desc in descs:
            state.track_node_from_action(
                getattr(desc, "package", None),
                getattr(desc, "plugin", None),
                getattr(desc, "node_name", getattr(desc, "name", None)),
            )

    # IncludeLaunchDescription (real)
    if cls_name == "IncludeLaunchDescription":
        src = getattr(action, "_launch_description_source", None)
        if src:
            loc = getattr(src, "location", None)
            if callable(loc) and context:
                try:
                    loc = loc(context)
                except Exception:
                    loc = None
            if loc:
                ros_ns = context._launch_configurations.get("ros_namespace") if context else None
                state.track_include(loc, ros_namespace=ros_ns)

    # Warn about action classes we do not recognise.
    if cls_name not in _KNOWN_UNTRACKED_CLASSES:
        logger.warning(
            "Unrecognised action type '%s' — any nodes or includes it "
            "declares may not appear in the resolved output",
            cls_name,
        )


# ─── Known untracked action class names ──────────────────────────────────────

# Real ROS 2 class names that _walk_untracked_action handles or that are safe
# to skip.  Our Action subclasses are dispatched via execute() and
# don't need to be listed here.
_KNOWN_UNTRACKED_CLASSES: frozenset = frozenset(
    {
        # Real launch_ros classes handled via cls_name duck-typing
        "Node",
        "LifecycleNode",
        "ComposableNodeContainer",
        "LoadComposableNodes",
        "IncludeLaunchDescription",
        # ROS 2 infrastructure — safe to skip (no nodes/includes produced)
        "LogInfo",
        "RegisterEventHandler",
        "EmitEvent",
        "Shutdown",
        "ExecuteProcess",
        "DeclareLaunchArgument",
        "SetLaunchConfiguration",
        "SetParameter",
        "PushRosNamespace",
        "ResetLaunchConfigurations",
        "AppendEnvironmentVariable",
        "SetEnvironmentVariable",
        "UnsetEnvironmentVariable",
        "OpaqueCoroutine",
        "OpaqueFunction",
        "GroupAction",
        "TimerAction",
    }
)

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
    state.tracked["nodes"] = []
    state.tracked["declared_args"] = []
    state.tracked["declared_args_by_file"] = {}
    state.tracked["global_params"] = []
    state.tracked["include_args"] = {}
    state.tracked["param_files"] = []
    state.tracked["set_launch_configurations"] = {}
    state.tracked["include_deps"] = []
    state.tracked["param_file_deps"] = []
    state.tracked["event_handlers"] = []

    # Workflow flags
    if workflow_options is not None:
        state.apply_opaque_file_access = bool(
            getattr(workflow_options, "apply_opaque_file_access", False)
        )
        state.preview_mode = bool(getattr(workflow_options, "preview", True))
        state.inline_params = bool(getattr(workflow_options, "inline_params", False))
        state.rosdep_fallback = bool(getattr(workflow_options, "rosdep_fallback", False))
        state.apply_arg_defaults = bool(getattr(workflow_options, "apply_arg_defaults", False))
        state.allow_unportable_paths = bool(
            getattr(workflow_options, "allow_unportable_paths", False)
        )
    else:
        state.apply_opaque_file_access = False
        state.preview_mode = True
        state.inline_params = False
        state.rosdep_fallback = False
        state.apply_arg_defaults = False
        state.allow_unportable_paths = False

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
            return _tracked_to_parsed_launch_file(state.tracked)

        if launch_file_str.endswith((".yaml", ".yml")):
            elements = parse_yaml_launch(content, launch_file_str)
        else:
            elements = parse_xml_launch(content, launch_file_str)
        subst_ctx = LaunchContext(state)
        subst_ctx._launch_configurations = dict(args_dict)
        subst_ctx.launch_file_dir = os.path.dirname(os.path.abspath(launch_file_str))
        subst_ctx.preview_mode = state.preview_mode
        resolve_xml_elements(elements, subst_ctx, include_stack=[launch_file_str])
        return _tracked_to_parsed_launch_file(state.tracked)

    # ── Python launch files ──────────────────────────────────────────────
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file_str)
    if spec is None or spec.loader is None:
        logger.error("cannot load %s", launch_file_str)
        return _tracked_to_parsed_launch_file(state.tracked)

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.error("Error loading launch file: %s", e)
        return _tracked_to_parsed_launch_file(state.tracked)

    if not hasattr(mod, "generate_launch_description"):
        logger.error("No generate_launch_description() function found")
        return _tracked_to_parsed_launch_file(state.tracked)

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
        return _tracked_to_parsed_launch_file(state.tracked)

    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, DeclareLaunchArgument):
            _apply_declared_arg(entity, ctx)

    _walk_actions(state, entities, ctx)

    return _tracked_to_parsed_launch_file(state.tracked)


def _tracked_to_parsed_launch_file(tracked: dict[str, Any]) -> Any:
    """Convert the internal _state.tracked dict to a ParsedLaunchFile."""
    from pathlib import Path as _Path

    from launch_plus.types import (
        ComposablePlugin as _ComposablePlugin,
    )
    from launch_plus.types import (
        DependencyKind as _DependencyKind,
    )
    from launch_plus.types import (
        EventHandlerKind as _EventHandlerKind,
    )
    from launch_plus.types import (
        FileDependency as _FileDependency,
    )
    from launch_plus.types import (
        LaunchInclude as _LaunchInclude,
    )
    from launch_plus.types import (
        NodeKindTag as _NodeKindTag,
    )
    from launch_plus.types import (
        ParamFileInlined as _ParamFileInlined,
    )
    from launch_plus.types import (
        ParamFileReference as _ParamFileReference,
    )
    from launch_plus.types import (
        ParsedLaunchFile as _ParsedLaunchFile,
    )
    from launch_plus.types import (
        ResolvedEventAction as _ResolvedEventAction,
    )
    from launch_plus.types import (
        ResolvedNode as _ResolvedNode,
    )
    # _effective_namespace is imported at module level from xml_resolver

    # ── Convert nodes ────────────────────────────────────────────────────
    resolved_nodes: list[_ResolvedNode] = []
    for n in tracked.get("nodes", []):
        kind_str = n.get("kind", "node")
        if kind_str == "set_parameter":
            continue

        ros_ns = n.get("ros_namespace")
        explicit_ns = n.get("explicit_namespace")
        eff_ns = _ros2_namespace_join(ros_ns, explicit_ns) if explicit_ns else ros_ns
        name = n.get("name", "")

        # Map kind
        kind_tag: _NodeKindTag
        plugins: list[_ComposablePlugin] = []
        load_target: str | None = None
        log_message: str | None = None
        remap_from: str | None = None
        remap_to: str | None = None
        exec_cmd: str | None = None
        exec_name: str | None = None
        handler_kind: _EventHandlerKind | None = None
        handler_target: str | None = None
        handler_target_node: str | None = None
        handler_namespace: str | None = None
        handler_start_state: str | None = None
        handler_goal_state: str | None = None
        handler_actions: list[_ResolvedEventAction] = []

        if kind_str == "container":
            kind_tag = _NodeKindTag.CONTAINER
            plugins = _convert_plugins(n.get("plugins", []))
        elif kind_str == "load_composable":
            kind_tag = _NodeKindTag.LOAD_COMPOSABLE
            load_target = n.get("target", "")
            plugins = _convert_plugins(n.get("plugins", []))
        elif kind_str == "lifecycle_node":
            kind_tag = _NodeKindTag.LIFECYCLE_NODE
        elif kind_str == "set_remap":
            kind_tag = _NodeKindTag.SET_REMAP
            remap_from = n.get("remap_from", "")
            remap_to = n.get("remap_to", "")
        elif kind_str == "log":
            kind_tag = _NodeKindTag.LOG
            log_message = n.get("message", "")
        elif kind_str == "executable":
            kind_tag = _NodeKindTag.EXECUTABLE
            exec_cmd = n.get("cmd", "")
            exec_name = name if name else None
        elif kind_str == "event_handler":
            kind_tag = _NodeKindTag.EVENT_HANDLER
            hk_str = n.get("handler_kind", "")
            hk_map = {
                "on_process_start": _EventHandlerKind.ON_PROCESS_START,
                "on_process_exit": _EventHandlerKind.ON_PROCESS_EXIT,
                "on_state_transition": _EventHandlerKind.ON_STATE_TRANSITION,
                "on_shutdown": _EventHandlerKind.ON_SHUTDOWN,
            }
            handler_kind = hk_map.get(hk_str)
            if handler_kind is None:
                continue
            handler_target = n.get("target")
            handler_target_node = n.get("target_node")
            handler_namespace = eff_ns
            handler_start_state = n.get("start_state")
            handler_goal_state = n.get("goal_state")
            handler_actions = [
                _ResolvedEventAction(
                    event=a.get("event", ""),
                    target_node=a.get("target_node"),
                    namespace=a.get("explicit_namespace"),
                )
                for a in n.get("eh_actions", [])
            ]
        else:
            kind_tag = _NodeKindTag.NODE

        # Include chain
        chain_raw = n.get("include_chain", [])
        include_chain = [(pkg, _Path(sp)) for pkg, sp in chain_raw]
        source = include_chain[-1] if include_chain else None

        # Param files
        param_files: list[_ParamFileReference | _ParamFileInlined] = []
        for pf in n.get("param_files", []):
            if pf.get("params") is not None:
                param_files.append(_ParamFileInlined(display=pf["path"], params=pf["params"]))
            else:
                param_files.append(_ParamFileReference(display=pf["path"], abs=pf["path"]))

        resolved_nodes.append(
            _ResolvedNode(
                package=n.get("package", ""),
                executable=n.get("executable", ""),
                name=name if name else None,
                namespace=eff_ns,
                explicit_namespace=explicit_ns,
                namespace_stack=[ros_ns] if ros_ns else [],
                parameters=dict(n.get("parameters", {})),
                remappings=[(f, t) for f, t in n.get("remappings", [])],
                env=dict(n.get("env", {})),
                source=source,
                include_chain=include_chain,
                kind=kind_tag,
                plugins=plugins,
                load_target=load_target,
                log_message=log_message,
                remap_from=remap_from,
                remap_to=remap_to,
                exec_cmd=exec_cmd,
                exec_name=exec_name,
                handler_kind=handler_kind,
                handler_target=handler_target,
                handler_target_node=handler_target_node,
                handler_namespace=handler_namespace,
                handler_start_state=handler_start_state,
                handler_goal_state=handler_goal_state,
                handler_actions=handler_actions,
                param_files=param_files,
                output=n.get("output"),
                args=n.get("args"),
                respawn=n.get("respawn"),
                respawn_delay=n.get("respawn_delay"),
            )
        )

    # ── Legacy event handlers ────────────────────────────────────────────
    has_inline_eh = any(n.kind == _NodeKindTag.EVENT_HANDLER for n in resolved_nodes)
    if not has_inline_eh:
        hk_map = {
            "on_process_start": _EventHandlerKind.ON_PROCESS_START,
            "on_process_exit": _EventHandlerKind.ON_PROCESS_EXIT,
            "on_state_transition": _EventHandlerKind.ON_STATE_TRANSITION,
            "on_shutdown": _EventHandlerKind.ON_SHUTDOWN,
        }
        for eh in tracked.get("event_handlers", []):
            hk = hk_map.get(eh.get("handler_kind", ""))
            if hk is None:
                continue
            ros_ns = eh.get("ros_namespace")
            explicit_ns = eh.get("explicit_namespace")
            handler_ns = _ros2_namespace_join(ros_ns, explicit_ns) if explicit_ns else ros_ns
            actions = [
                _ResolvedEventAction(
                    event=a.get("event", ""),
                    target_node=a.get("target_node"),
                    namespace=a.get("explicit_namespace"),
                )
                for a in eh.get("actions", [])
            ]
            resolved_nodes.append(
                _ResolvedNode(
                    kind=_NodeKindTag.EVENT_HANDLER,
                    namespace_stack=[ros_ns] if ros_ns else [],
                    explicit_namespace=explicit_ns,
                    handler_kind=hk,
                    handler_target=eh.get("target"),
                    handler_target_node=eh.get("target_node"),
                    handler_namespace=handler_ns,
                    handler_start_state=eh.get("start_state"),
                    handler_goal_state=eh.get("goal_state"),
                    handler_actions=actions,
                )
            )

    # ── Convert include deps ─────────────────────────────────────────────
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

    # ── Declared args ────────────────────────────────────────────────────
    declared_arg_defaults = {a["name"]: a["default"] for a in tracked.get("declared_args", [])}

    # ── Param file deps ──────────────────────────────────────────────────
    param_file_deps = [
        _FileDependency(
            package=dep["package"],
            share_path=_Path(dep["share_path"]),
            kind=_DependencyKind.PARAM,
        )
        for dep in tracked.get("param_file_deps", [])
    ]

    # ── Per-file declared args ───────────────────────────────────────────
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
        nodes=resolved_nodes,
        launch_includes=launch_includes,
        param_files=param_file_deps,
        declared_arg_defaults=declared_arg_defaults,
        declared_args_by_file=declared_args_by_file,
        global_params=list(tracked.get("global_params", [])),
    )


def _convert_plugins(raw_plugins: list[dict]) -> list:
    """Convert raw plugin dicts to ComposablePlugin dataclasses."""
    from launch_plus.types import (
        ComposablePlugin as _ComposablePlugin,
    )
    from launch_plus.types import (
        ParamFileInlined as _ParamFileInlined,
    )
    from launch_plus.types import (
        ParamFileReference as _ParamFileReference,
    )

    result = []
    for p in raw_plugins:
        pf_list: list[_ParamFileReference | _ParamFileInlined] = []
        for pf in p.get("param_files", []):
            if pf.get("params") is not None:
                pf_list.append(_ParamFileInlined(display=pf["path"], params=pf["params"]))
            else:
                pf_list.append(_ParamFileReference(display=pf["path"], abs=pf["path"]))
        result.append(
            _ComposablePlugin(
                package=p.get("package", ""),
                plugin=p.get("plugin", ""),
                name=p.get("name"),
                parameters=dict(p.get("parameters", {})),
                remappings=[(f, t) for f, t in p.get("remappings", [])],
                param_files=pf_list,
            )
        )
    return result
