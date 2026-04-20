#!/usr/bin/env python3
"""
roscope resolver.

Resolves ROS 2 launch files (XML, YAML, and Python) by parsing their
structure, evaluating substitutions, and tracking nodes, includes, and
package dependencies.  Python launch files are handled via import-patching
hooks that intercept ``launch_ros`` and ``launch`` classes.

The main entry point is :func:`resolve_file`, which returns a
:class:`~roscope.types.ParsedLaunchFile`.
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

from roscope.entities.action import Action
from roscope.entities.actions.arg import DeclareLaunchArgument, _apply_declared_arg
from roscope.entities.actions.composable_node_container import ComposableNodeContainer
from roscope.entities.actions.env import (
    PushRosNamespace,
    SetEnvironmentVariable,
    UnsetEnvironmentVariable,
)
from roscope.entities.actions.event_handler import (
    OnProcessExit,
    OnProcessStart,
    OnShutdown,
    OnStateTransition,
    RegisterEventHandler,
    Shutdown,
    TrackedEmitEvent,
)
from roscope.entities.actions.executable import ExecuteProcess
from roscope.entities.actions.group import GroupAction, OpaqueFunction, TimerAction
from roscope.entities.actions.include import IncludeLaunchDescription
from roscope.entities.actions.load_composable_nodes import LoadComposableNodes
from roscope.entities.actions.node import LifecycleNode, Node
from roscope.entities.actions.param import ParameterFile, SetLaunchConfiguration, SetParameter
from roscope.entities.conditions import (
    IfCondition,
    LaunchConfigurationEquals,
    LaunchConfigurationNotEquals,
    UnlessCondition,
)
from roscope.entities.descriptions import ComposableNode
from roscope.entities.launch_description import LaunchDescription as _LaunchDescription
from roscope.entities.launch_description_sources import (
    AnyLaunchDescriptionSource,
    PythonLaunchDescriptionSource,
    XMLLaunchDescriptionSource,
)
from roscope.entities.parsing import _ActionParser
from roscope.entities.state import LaunchContext, ResolverState
from roscope.entities.substitutions.env import EnvironmentVariable as _EnvironmentVariable
from roscope.entities.substitutions.find_pkg_share import FindPackageShare
from roscope.entities.substitutions.launch_config import LaunchConfiguration
from roscope.entities.substitutions.path_join import PathJoinSubstitution
from roscope.parsers.entity import Entity
from roscope.parsers.xml_parser import parse_xml_launch as _parse_xml_launch_entity
from roscope.parsers.yaml_parser import parse_yaml_launch as _parse_yaml_launch_entity

logger = logging.getLogger("roscope")


# ─── Shim helpers ─────────────────────────────────────────────────────────────


def _make_shim_getattr(module_name: str, *, is_substitution_module: bool = False):
    """Return a ``__getattr__`` for shim modules.

    When a Python launch file does ``from launch.actions import UnknownClass``
    and the class is absent from our shim, Python falls through to
    ``module.__getattr__('UnknownClass')``.  Without this hook the import
    raises ``ImportError`` and the entire file's topology is lost.

    The hook returns a stub class that:
    - subclasses ``Action`` (for action modules) or ``Substitution`` (for
      substitution modules) so type checks in the resolver behave correctly
    - preserves the ``condition`` kwarg so ``Action.visit()`` still gates
      execution correctly even for unknown actions
    - warns once per unknown name so the user knows topology may be incomplete
    """
    from roscope.entities.substitution import Substitution

    _warned: set[str] = set()

    def __getattr__(attr_name: str):  # noqa: N807
        if attr_name.startswith("__"):
            raise AttributeError(attr_name)

        if attr_name not in _warned:
            logger.warning(
                "unimplemented shim: %s.%s — topology that depends on this "
                "class will be missing from the resolved output",
                module_name,
                attr_name,
            )
            _warned.add(attr_name)

        if is_substitution_module:

            class _UnimplementedShim(Substitution):  # type: ignore[valid-type]
                def __init__(self, *_a, **_kw):
                    pass

                def perform(self, context) -> str:
                    return ""

        else:

            class _UnimplementedShim(Action):  # type: ignore[no-redef]
                def __init__(self, *_a, **_kw):
                    super().__init__(condition=_kw.get("condition"))

                def execute(self, context) -> list:
                    return []

                def perform(self, context) -> str:
                    return ""

        _UnimplementedShim.__name__ = attr_name
        _UnimplementedShim.__qualname__ = attr_name
        return _UnimplementedShim

    return __getattr__


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
    mod.__getattr__ = _make_shim_getattr("launch_ros.actions")
    return mod


def _build_patched_launch_ros_utilities():
    from roscope.entities.utilities.namespace_utils import (
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
    mod.EnvironmentVariable = _EnvironmentVariable
    mod.TextSubstitution = lambda text="", **kw: str(text)
    mod.PythonExpression = lambda expression=None, **kw: None
    mod.__getattr__ = _make_shim_getattr("launch.substitutions", is_substitution_module=True)
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
    mod.__getattr__ = _make_shim_getattr("launch.actions")
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


def _build_patched_launch_events():
    mod = types.ModuleType("launch.events")
    mod.__path__ = []
    mod.__package__ = "launch.events"
    # matches_action is used in event-handler callbacks at runtime; a no-op lambda
    # is sufficient for static resolution — we only need the import to succeed.
    mod.matches_action = lambda *_a, **_kw: lambda _e: True
    mod.Shutdown = Shutdown
    return mod


def _build_patched_launch_conditions():
    mod = types.ModuleType("launch.conditions")
    mod.IfCondition = IfCondition
    mod.UnlessCondition = UnlessCondition
    mod.LaunchConfigurationEquals = LaunchConfigurationEquals
    mod.LaunchConfigurationNotEquals = LaunchConfigurationNotEquals
    return mod


def _build_patched_launch_launch_description_source():
    from roscope.entities.launch_description_source import LaunchDescriptionSource

    mod = types.ModuleType("launch.launch_description_source")
    mod.LaunchDescriptionSource = LaunchDescriptionSource
    return mod


def _build_patched_launch_launch_description_sources():
    from roscope.entities.launch_description_sources import FrontendLaunchDescriptionSource

    mod = types.ModuleType("launch.launch_description_sources")
    mod.PythonLaunchDescriptionSource = PythonLaunchDescriptionSource
    mod.AnyLaunchDescriptionSource = AnyLaunchDescriptionSource
    mod.FrontendLaunchDescriptionSource = FrontendLaunchDescriptionSource
    return mod


def _build_patched_launch_xml():
    mod = types.ModuleType("launch_xml")
    mod.__path__ = []
    mod.__package__ = "launch_xml"
    return mod


def _build_patched_launch_xml_launch_description_sources():
    mod = types.ModuleType("launch_xml.launch_description_sources")
    mod.XMLLaunchDescriptionSource = XMLLaunchDescriptionSource
    return mod


# ── Per-class submodule shims ─────────────────────────────────────────────────
# Upstream code may import via the per-class submodule path, e.g.:
#   from launch.launch_description_sources.python_launch_description_source \
#       import PythonLaunchDescriptionSource


def _build_patched_launch_lds_python():
    mod_name = "launch.launch_description_sources.python_launch_description_source"
    mod = types.ModuleType(mod_name)
    mod.PythonLaunchDescriptionSource = PythonLaunchDescriptionSource
    return mod


def _build_patched_launch_lds_any():
    mod_name = "launch.launch_description_sources.any_launch_description_source"
    mod = types.ModuleType(mod_name)
    mod.AnyLaunchDescriptionSource = AnyLaunchDescriptionSource
    return mod


def _build_patched_launch_lds_frontend():
    from roscope.entities.launch_description_sources import FrontendLaunchDescriptionSource

    mod_name = "launch.launch_description_sources.frontend_launch_description_source"
    mod = types.ModuleType(mod_name)
    mod.FrontendLaunchDescriptionSource = FrontendLaunchDescriptionSource
    return mod


def _build_patched_launch_xml_lds_xml():
    mod_name = "launch_xml.launch_description_sources.xml_launch_description_source"
    mod = types.ModuleType(mod_name)
    mod.XMLLaunchDescriptionSource = XMLLaunchDescriptionSource
    return mod


def _build_patched_launch_ros_substitutions():
    from roscope.entities.substitutions.executable_in_package import ExecutableInPackage

    mod = types.ModuleType("launch_ros.substitutions")
    mod.FindPackageShare = FindPackageShare
    mod.ExecutableInPackage = ExecutableInPackage
    mod.__getattr__ = _make_shim_getattr("launch_ros.substitutions", is_substitution_module=True)
    return mod


def _build_patched_launch_ros_events():
    mod = types.ModuleType("launch_ros.events")
    mod.__path__ = []
    mod.__package__ = "launch_ros.events"
    return mod


def _build_patched_launch_ros_events_lifecycle():
    mod = types.ModuleType("launch_ros.events.lifecycle")
    mod.ChangeState = lambda *_a, **_kw: None
    return mod


def _build_patched_launch_ros_event_handlers():
    mod = types.ModuleType("launch_ros.event_handlers")
    mod.OnStateTransition = OnStateTransition
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
        "launch.events": _build_patched_launch_events,
        "launch.event_handlers": _build_patched_launch_event_handlers,
        "launch.conditions": _build_patched_launch_conditions,
        "launch.launch_description_source": _build_patched_launch_launch_description_source,
        "launch.launch_description_sources": _build_patched_launch_launch_description_sources,
        "launch_xml": _build_patched_launch_xml,
        "launch_xml.launch_description_sources": _build_patched_launch_xml_launch_description_sources,
        "launch_xml.launch_description_sources.xml_launch_description_source": _build_patched_launch_xml_lds_xml,
        "launch.launch_description_sources.python_launch_description_source": _build_patched_launch_lds_python,
        "launch.launch_description_sources.any_launch_description_source": _build_patched_launch_lds_any,
        "launch.launch_description_sources.frontend_launch_description_source": _build_patched_launch_lds_frontend,
        "launch_ros.events": _build_patched_launch_ros_events,
        "launch_ros.events.lifecycle": _build_patched_launch_ros_events_lifecycle,
        "launch_ros.event_handlers": _build_patched_launch_ros_event_handlers,
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
        if "." in fullname:
            parent_name, _, child_name = fullname.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child_name, mod)
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
    from roscope.entities.expose import action_parse_methods

    tag = elem.type_name
    if tag in action_parse_methods:
        parser = _ActionParser(ctx, include_stack)
        action = action_parse_methods[tag](elem, parser)
        if isinstance(action, Action):
            return action.visit(ctx) or []
        return []
    logger.warning("unknown element: <%s>", tag)
    return []


def _execute_actions(actions, context) -> list:
    """Execute a list of actions and collect resolved results."""
    results: list = []
    for action in actions or []:
        if action is None:
            continue
        if not isinstance(action, Action):
            logger.error("expected Action, got %s", type(action).__name__)
            continue
        children = action.visit(context)
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

    state.root_source_key = launch_file_str

    state.fetched_packages.clear()
    state.declared_arg_names.clear()
    state.include_chain.clear()
    state.package_shares = dict(package_shares)

    # Reset dependency tracking
    state.packages = []
    state.include_deps = []
    state.param_file_deps = []

    # Workflow flags
    if workflow_options is not None:
        state.preview_mode = bool(getattr(workflow_options, "preview", True))
        state.rosdep_fallback = bool(getattr(workflow_options, "rosdep_fallback", False))
        state.show_empty_includes = bool(getattr(workflow_options, "show_empty_includes", False))
        state.show_args = bool(getattr(workflow_options, "show_args", False))
    else:
        state.preview_mode = True
        state.rosdep_fallback = False
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
        if "." in mod_name:
            parent_name, _, child_name = mod_name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child_name, sys.modules[mod_name])

    # ── Resolve by file type ─────────────────────────────────────────────
    if launch_file_str.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
        try:
            with open(launch_file_str) as f:
                content = f.read()
        except Exception as e:
            logger.error("cannot read %s: %s", launch_file_str, e)
            return _tracked_to_parsed_launch_file(state), []

        if launch_file_str.endswith((".yaml", ".yml")):
            elements = parse_yaml_launch(content, launch_file_str)
        else:
            elements = parse_xml_launch(content, launch_file_str)
        subst_ctx = LaunchContext(state)
        subst_ctx._launch_configurations = dict(args_dict)
        subst_ctx.launch_file_dir = os.path.dirname(os.path.abspath(launch_file_str))
        subst_ctx.preview_mode = state.preview_mode
        resolved = resolve_xml_elements(elements, subst_ctx, include_stack=[launch_file_str])
        return _tracked_to_parsed_launch_file(state, subst_ctx), resolved

    # ── Python launch files ──────────────────────────────────────────────
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file_str)
    if spec is None or spec.loader is None:
        logger.error("cannot load %s", launch_file_str)
        return _tracked_to_parsed_launch_file(state), []

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.error("Error loading launch file: %s", e)
        return _tracked_to_parsed_launch_file(state), []

    if not hasattr(mod, "generate_launch_description"):
        logger.error("No generate_launch_description() function found")
        return _tracked_to_parsed_launch_file(state), []

    # Inject persisted global params
    if "__global_params__" in args_dict:
        try:
            persisted_global_params = json.loads(args_dict.pop("__global_params__"))
        except Exception:
            pass

    ctx = LaunchContext(state)
    ctx._launch_configurations = dict(args_dict)

    if persisted_global_params:
        gp_tuples = [(entry[0], entry[1]) for entry in persisted_global_params if len(entry) == 2]
        ctx._launch_configurations["global_params"] = list(gp_tuples)

    try:
        ld = mod.generate_launch_description()
    except Exception as e:
        logger.error("generate_launch_description() failed: %s", e)
        return _tracked_to_parsed_launch_file(state), []

    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, DeclareLaunchArgument):
            _apply_declared_arg(entity, ctx)

    from roscope.entities.actions.group import GroupAction as _GroupAction

    resolved = _GroupAction(actions=list(entities), scoped=False).visit(ctx) or []

    return _tracked_to_parsed_launch_file(state, ctx), resolved


def _tracked_to_parsed_launch_file(state: Any, ctx: Any = None) -> Any:
    """Build a ParsedLaunchFile from resolver state."""
    from roscope.types import (
        DependencyKind as _DependencyKind,
    )
    from roscope.types import (
        FileDependency as _FileDependency,
    )
    from roscope.types import (
        LaunchInclude as _LaunchInclude,
    )
    from roscope.types import (
        ParsedLaunchFile as _ParsedLaunchFile,
    )

    # ── Include deps ─────────────────────────────────────────────────
    launch_includes: list[_LaunchInclude] = [
        _LaunchInclude(
            package=dep["package"],
            share_path=Path(dep["share_path"]),
            explicit_args=dep.get("include_args", {}),
            namespace_stack=[dep["ros_namespace"]] if dep.get("ros_namespace") else [],
        )
        for dep in state.include_deps
    ]

    # ── Param file deps ───────────────────────────────────────────────
    param_file_deps = [
        _FileDependency(
            package=dep["package"],
            share_path=Path(dep["share_path"]),
            kind=_DependencyKind.PARAM,
        )
        for dep in state.param_file_deps
    ]

    # ── Global params — read from context (single source of truth) ────
    global_params: list = []
    if ctx is not None:
        lc = getattr(ctx, "_launch_configurations", {})
        global_params = list(lc.get("global_params", []))

    return _ParsedLaunchFile(
        packages=list(state.packages),
        launch_includes=launch_includes,
        param_files=param_file_deps,
        global_params=global_params,
    )
