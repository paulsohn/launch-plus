"""Import patcher: intercept ``import launch`` / ``import launch_ros`` and
provide shim modules that redirect to our entity implementations.

This module focuses on RE-EXPORTING — implementations live in their own
files under ``entities/``.  The only exception is ``ament_index_python``
which is trivially stubbed inline.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import types
from pathlib import Path

import launch_plus.resolver as _R
from launch_plus.entities.actions.arg import DeclareLaunchArgument
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
from launch_plus.entities.actions.group import (
    GroupAction,
    OpaqueFunction,
    TimerAction,
)
from launch_plus.entities.actions.include import IncludeLaunchDescription
from launch_plus.entities.actions.node import (
    ComposableNode,
    ComposableNodeContainer,
    LifecycleNode,
    LoadComposableNodes,
    Node,
)
from launch_plus.entities.actions.param import (
    ParameterFile,
    SetLaunchConfiguration,
    SetParameter,
)
from launch_plus.entities.conditions import (
    IfCondition,
    LaunchConfigurationEquals,
    LaunchConfigurationNotEquals,
    UnlessCondition,
)
from launch_plus.entities.launch_description import LaunchDescription as _LaunchDescription
from launch_plus.entities.launch_description_source import (
    AnyLaunchDescriptionSource,
    PythonLaunchDescriptionSource,
)
from launch_plus.entities.state import LaunchContext
from launch_plus.entities.substitutions.environment_variable import DeferredEnvironmentVariable
from launch_plus.entities.substitutions.find_pkg_share import FindPackageShare
from launch_plus.entities.substitutions.launch_config import LaunchConfiguration
from launch_plus.entities.substitutions.path_join import PathJoinSubstitution

logger = logging.getLogger("launch_plus")

# ─── ROS prefix fallback ─────────────────────────────────────────────────────
_ROS_DISTRO = os.environ.get("ROS_DISTRO", "")
_ROS_DISTRO_PREFIX = f"/opt/ros/{_ROS_DISTRO}" if _ROS_DISTRO else ""


# ─── Module builders ─────────────────────────────────────────────────────────


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


def _build_patched_ament_index_python():
    """Inline stub — ament_index_python has no entities/ counterpart."""
    mod = types.ModuleType("ament_index_python")
    mod.__path__ = []
    mod.__package__ = "ament_index_python"
    return mod


def _build_patched_ament_index_python_packages():
    mod = types.ModuleType("ament_index_python.packages")

    def get_package_share_directory(package_name):
        logger.warning(
            "get_package_share_directory('%s') is non-idiomatic; "
            "prefer FindPackageShare('%s') from launch_ros.substitutions",
            package_name,
            package_name,
        )
        state = _R.get_state()
        state.track_package(package_name)
        return state.resolve_pkg_share(package_name)

    def get_package_prefix(package_name):
        share = _R.get_state().resolve_pkg_share(package_name)
        if share.startswith("$("):
            return _ROS_DISTRO_PREFIX
        p = Path(share)
        return str(p.parent) if p.parent != p else _ROS_DISTRO_PREFIX

    mod.get_package_share_directory = get_package_share_directory
    mod.get_package_prefix = get_package_prefix
    return mod


# ─── PatchingFinder ──────────────────────────────────────────────────────────


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
        "ament_index_python": _build_patched_ament_index_python,
        "ament_index_python.packages": _build_patched_ament_index_python_packages,
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
