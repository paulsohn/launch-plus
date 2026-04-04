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

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

from launch_plus.entities.actions.arg import DeclareLaunchArgument, _apply_declared_arg
from launch_plus.entities.actions.env import (
    PushRosNamespace,
    SetEnvironmentVariable,
    UnsetEnvironmentVariable,
)
from launch_plus.entities.actions.event_handler import (
    OnProcessExit,
    OnProcessStart,
    OnShutdown,
    OnStateTransition,
    RegisterEventHandler,
    Shutdown,
    TrackedEmitEvent,
)
from launch_plus.entities.actions.executable import ExecuteProcess
from launch_plus.entities.actions.group import GroupAction, OpaqueFunction, TimerAction
from launch_plus.entities.actions.include import IncludeLaunchDescription
from launch_plus.entities.actions.node import (
    ComposableNode,
    ComposableNodeContainer,
    LifecycleNode,
    LoadComposableNodes,
    Node,
)
from launch_plus.entities.actions.param import ParameterFile, SetLaunchConfiguration, SetParameter
from launch_plus.entities.conditions import (
    IfCondition,
    LaunchConfigurationEquals,
    LaunchConfigurationNotEquals,
    UnlessCondition,
)
from launch_plus.entities.helpers import _extract_pkg_and_share_path
from launch_plus.entities.launch_description import LaunchDescription as _LaunchDescription
from launch_plus.entities.launch_description_source import (
    AnyLaunchDescriptionSource,
    PythonLaunchDescriptionSource,
)
from launch_plus.entities.parsing import _ActionParser
from launch_plus.entities.state import LaunchContext, ResolverState
from launch_plus.entities.substitutions.environment_variable import DeferredEnvironmentVariable
from launch_plus.entities.substitutions.find_pkg_share import FindPackageShare
from launch_plus.entities.substitutions.launch_config import LaunchConfiguration
from launch_plus.entities.substitutions.path_join import PathJoinSubstitution
from launch_plus.parsers.entity import Entity
from launch_plus.parsers.xml_parser import parse_xml_launch as _parse_xml_launch_entity
from launch_plus.parsers.yaml_parser import parse_yaml_launch as _parse_yaml_launch_entity

logger = logging.getLogger("launch_plus")


# ─── Import system patcher ──────────────────────────────��─────────────────────
#
# Intercept ``import launch`` / ``import launch_ros`` and provide shim modules
# that redirect to our entity implementations.


def _build_patched_launch():
    mod = types.ModuleType("launch")
    mod.__path__ = []
    mod.__package__ = "launch"
    mod.LaunchDescription = _LaunchDescription
    mod.LaunchContext = LaunchContext
    return mod


def _build_patched_launch_ros():
    mod = types.ModuleType("launch_ros")
    mod.__path__ = []
    mod.__package__ = "launch_ros"
    return mod


def _build_patched_launch_ros_actions():
    mod = types.ModuleType("launch_ros.actions")
    mod.Node = Node
    mod.LifecycleNode = LifecycleNode
    mod.ComposableNodeContainer = ComposableNodeContainer
    mod.LoadComposableNodes = LoadComposableNodes
    mod.SetParameter = SetParameter
    mod.SetRemap = lambda *a, **kw: None
    mod.PushRosNamespace = PushRosNamespace
    mod.SetParametersCallback = lambda *a, **kw: None
    return mod


def _build_patched_launch_ros_utilities():
    from launch_plus.entities.utilities.namespace_utils import (
        make_namespace_absolute,
        prefix_namespace,
    )

    mod = types.ModuleType("launch_ros.utilities")
    mod.make_namespace_absolute = make_namespace_absolute
    mod.prefix_namespace = prefix_namespace
    mod.get_node_name_count = lambda *a, **kw: 0
    mod.evaluate_parameters = lambda *a, **kw: []
    mod.normalize_parameters = lambda *a, **kw: []
    mod.add_node_name_count_to_name = lambda name, **kw: name
    return mod


def _build_patched_launch_ros_descriptions():
    mod = types.ModuleType("launch_ros.descriptions")
    mod.ComposableNode = ComposableNode
    mod.ParameterFile = ParameterFile
    return mod


def _build_patched_launch_substitutions():
    mod = types.ModuleType("launch.substitutions")
    mod.__path__ = []
    mod.FindPackageShare = FindPackageShare
    mod.PathJoinSubstitution = PathJoinSubstitution
    mod.LaunchConfiguration = LaunchConfiguration
    mod.EnvironmentVariable = DeferredEnvironmentVariable
    mod.TextSubstitution = lambda text="", **kw: str(text)
    mod.PythonExpression = lambda expression=None, **kw: None
    return mod


def _build_patched_launch_substitutions_environment_variable():
    parent = sys.modules.get("launch.substitutions")
    if parent is None:
        parent = _build_patched_launch_substitutions()
    mod = types.ModuleType("launch.substitutions.environment_variable")
    mod.EnvironmentVariable = parent.EnvironmentVariable
    return mod


def _build_patched_launch_actions():
    mod = types.ModuleType("launch.actions")
    mod.IncludeLaunchDescription = IncludeLaunchDescription
    mod.DeclareLaunchArgument = DeclareLaunchArgument
    mod.OpaqueFunction = OpaqueFunction
    mod.GroupAction = GroupAction
    mod.SetLaunchConfiguration = SetLaunchConfiguration
    mod.LogInfo = lambda *a, **kw: None
    mod.TimerAction = TimerAction
    mod.RegisterEventHandler = RegisterEventHandler
    mod.EmitEvent = TrackedEmitEvent
    mod.Shutdown = Shutdown
    mod.PushLaunchConfigurations = lambda *a, **kw: None
    mod.PopLaunchConfigurations = lambda *a, **kw: None
    mod.SetEnvironmentVariable = SetEnvironmentVariable
    mod.UnsetEnvironmentVariable = UnsetEnvironmentVariable
    mod.ExecuteProcess = ExecuteProcess
    mod.ExecuteLocal = lambda *a, **kw: None
    mod.OnProcessExit = OnProcessExit
    mod.OnProcessStart = OnProcessStart
    return mod


def _build_patched_launch_event_handlers():
    mod = types.ModuleType("launch.event_handlers")
    mod.OnProcessExit = OnProcessExit
    mod.OnProcessStart = OnProcessStart
    mod.OnProcessIO = lambda *a, **kw: None
    mod.OnShutdown = OnShutdown
    mod.OnStateTransition = OnStateTransition
    mod.OnExecutionComplete = lambda *a, **kw: None
    return mod


def _build_patched_launch_conditions():
    mod = types.ModuleType("launch.conditions")
    mod.IfCondition = IfCondition
    mod.UnlessCondition = UnlessCondition
    mod.LaunchConfigurationEquals = LaunchConfigurationEquals
    mod.LaunchConfigurationNotEquals = LaunchConfigurationNotEquals
    return mod


def _build_patched_launch_launch_description_sources():
    mod = types.ModuleType("launch.launch_description_sources")
    mod.PythonLaunchDescriptionSource = PythonLaunchDescriptionSource
    mod.AnyLaunchDescriptionSource = AnyLaunchDescriptionSource
    return mod


def _build_patched_launch_ros_substitutions():
    mod = types.ModuleType("launch_ros.substitutions")
    mod.FindPackageShare = FindPackageShare
    return mod


def _build_patched_launch_ros_parameter_descriptions():
    mod = types.ModuleType("launch_ros.parameter_descriptions")
    mod.ParameterFile = ParameterFile
    mod.ParameterDescription = lambda *a, **kw: None
    mod.ParameterValue = lambda *a, **kw: None
    return mod


_PATCHED_MODULES: dict[str, types.ModuleType] = {}


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Meta-path finder that returns pre-built shim modules for ROS 2 packages."""

    PATCHED: dict = {
        "launch": _build_patched_launch,
        "launch_ros": _build_patched_launch_ros,
        "launch_ros.actions": _build_patched_launch_ros_actions,
        "launch_ros.utilities": _build_patched_launch_ros_utilities,
        "launch_ros.descriptions": _build_patched_launch_ros_descriptions,
        "launch.substitutions": _build_patched_launch_substitutions,
        "launch.substitutions.environment_variable": (
            _build_patched_launch_substitutions_environment_variable
        ),
        "launch.actions": _build_patched_launch_actions,
        "launch.event_handlers": _build_patched_launch_event_handlers,
        "launch.conditions": _build_patched_launch_conditions,
        "launch.launch_description_sources": _build_patched_launch_launch_description_sources,
        "launch_ros.substitutions": _build_patched_launch_ros_substitutions,
        "launch_ros.parameter_descriptions": _build_patched_launch_ros_parameter_descriptions,
    }

    def find_module(self, fullname, path=None):
        if fullname in self.PATCHED:
            return self
        return None

    def load_module(self, fullname):
        if fullname in sys.modules:
            return sys.modules[fullname]
        if fullname not in _PATCHED_MODULES:
            _PATCHED_MODULES[fullname] = self.PATCHED[fullname]()
        mod = _PATCHED_MODULES[fullname]
        sys.modules[fullname] = mod
        return mod


# ─── XML/YAML parsing ────────────────────────────────────────────────────────


def parse_xml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse an XML launch file to a list of Entity objects."""
    return list(_parse_xml_launch_entity(content, file_path))


def parse_yaml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse a YAML launch file to a list of Entity objects."""
    return list(_parse_yaml_launch_entity(content, file_path))


# ─── XML/YAML element resolution ────────��────────────────────────────────────


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


# ─── Main entry point ─────────────────────────────────────────────���──────────


def resolve_file(
    launch_file: Path,
    args: dict[str, str],
    package_shares: dict[str, str],
    fetch_dir: Path,
    *,
    workflow_options: Any = None,
    lockfile: Any = None,
    global_params: list | None = None,
    fetch_options: Any = None,
) -> Any:
    """Resolve a launch file and return (ParsedLaunchFile, actions).

    Parameters
    ----------
    launch_file : Path
        Absolute path to the launch file.
    args : dict
        Launch arguments (``name:=value`` pairs).
    package_shares : dict
        Package name → share directory path.
    fetch_dir : Path
        Absolute path to the source/fetch directory.
    workflow_options : ResolveWorkflowOptions
        Workflow flags.
    lockfile : Lockfile
        Lockfile with package/repo info.
    fetch_options : FetchOptions | None
        Options for inline git sparse-checkout.
    global_params : list | None
        Persisted global params from prior files.
    """
    state = ResolverState()
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

    state.fetch_dir = str(fetch_dir)
    state.fetch_options = fetch_options

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

    ctx = LaunchContext()
    ctx._launch_configurations = dict(args_dict)

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

    from launch_plus.entities.actions.group import GroupAction as _GroupAction

    resolved = _GroupAction(actions=list(entities), scoped=False).execute(ctx)

    return _tracked_to_parsed_launch_file(state.tracked), resolved


def _tracked_to_parsed_launch_file(tracked: dict[str, Any]) -> Any:
    """Build a ParsedLaunchFile from tracked state."""
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

    # ── Include deps ────���─────────────────────────────────────────────
    launch_includes: list[_LaunchInclude] = []
    include_args_map = tracked.get("include_args", {})
    for dep in tracked.get("include_deps", []):
        dep_include_args = dep.get("include_args", {})
        if not dep_include_args:
            dep_include_args = include_args_map.get(dep.get("path", ""), {})
        launch_includes.append(
            _LaunchInclude(
                package=dep["package"],
                share_path=Path(dep["share_path"]),
                explicit_args=dep_include_args,
                namespace_stack=[dep["ros_namespace"]] if dep.get("ros_namespace") else [],
            )
        )

    # ── Declared args ───────────���─────────────────────────────────────
    declared_arg_defaults = {a["name"]: a["default"] for a in tracked.get("declared_args", [])}

    # ── Param file deps ────────���──────────────────────────────────────
    param_file_deps = [
        _FileDependency(
            package=dep["package"],
            share_path=Path(dep["share_path"]),
            kind=_DependencyKind.PARAM,
        )
        for dep in tracked.get("param_file_deps", [])
    ]

    # ── Per-file declared args ────────────��───────────────────────────
    declared_args_by_file: dict[tuple[str, Path], dict[str, str]] = {}
    for key_str, args_list in tracked.get("declared_args_by_file", {}).items():
        if "://" in key_str:
            idx = key_str.index("://")
            pkg = key_str[:idx]
            sp = Path(key_str[idx + 3 :])
        else:
            pkg = ""
            sp = Path(key_str)
        declared_args_by_file[(pkg, sp)] = {a["name"]: a["default"] for a in args_list}

    return _ParsedLaunchFile(
        packages=list(tracked.get("packages", [])),
        launch_includes=launch_includes,
        param_files=param_file_deps,
        declared_arg_defaults=declared_arg_defaults,
        declared_args_by_file=declared_args_by_file,
        global_params=list(tracked.get("global_params", [])),
    )
