"""Import system patcher for Python launch file resolution.

Intercepts imports of launch, launch_ros, ament_index_python,
and lifecycle_msgs modules, replacing them with lightweight stubs that
track package dependencies without requiring a full ROS 2 installation.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import logging
import os
import sys
import types
from pathlib import Path

import launch_plus.resolver as _R
from launch_plus.entities.actions.arg import _DeclaredArg
from launch_plus.entities.actions.env import (
    _TrackedPushRosNamespace,
    _TrackedSetEnvironmentVariable,
    _TrackedUnsetEnvironmentVariable,
)
from launch_plus.entities.actions.event_handler import (
    _TrackedChangeState,
    _TrackedEmitEvent,
    _TrackedMatchesAction,
    _TrackedOnProcessExit,
    _TrackedOnProcessStart,
    _TrackedOnShutdown,
    _TrackedOnStateTransition,
    _TrackedRegisterEventHandler,
    _TrackedShutdown,
)
from launch_plus.entities.actions.executable import _TrackedExecutable
from launch_plus.entities.actions.group import (
    _TimerAction,
    _TrackedGroupAction,
    _TrackedOpaqueFunction,
)
from launch_plus.entities.actions.include import _TrackedIncludeLaunchDescription
from launch_plus.entities.actions.node import (
    _TrackedComposableNode,
    _TrackedComposableNodeContainer,
    _TrackedLifecycleNode,
    _TrackedLoadComposableNodes,
    _TrackedNode,
)
from launch_plus.entities.actions.param import (
    _SetLaunchConfiguration,
    _TrackedParameterFile,
    _TrackedSetParameter,
)
from launch_plus.entities.helpers import _to_str
from launch_plus.entities.state import _StubLaunchContext
from launch_plus.entities.substitution import Substitution
from launch_plus.entities.substitutions.find_pkg_share import _TrackedFindPackageShare
from launch_plus.entities.substitutions.launch_config import _LaunchConfiguration
from launch_plus.entities.substitutions.path_join import _TrackedPathJoinSubstitution

logger = logging.getLogger("launch_plus")
_ROS_DISTRO = os.environ.get("ROS_DISTRO", "")
_ROS_DISTRO_PREFIX = f"/opt/ros/{_ROS_DISTRO}" if _ROS_DISTRO else ""


# ─── Import system patcher ────────────────────────────────────────────────────

_PATCHED_MODULES = {}


def _build_patched_launch():
    """Stub for the top-level `launch` package (used when ROS 2 is not installed)."""
    mod = types.ModuleType("launch")
    mod.__path__ = []  # mark as package so submodule imports work
    mod.__package__ = "launch"

    class _LaunchDescription:
        def __init__(self, entities=None, **kwargs):
            self.entities = list(entities or [])

        def add_action(self, action):
            self.entities.append(action)

    mod.LaunchDescription = _LaunchDescription
    mod.LaunchContext = _StubLaunchContext
    return mod


def _build_patched_launch_ros():
    """Stub for the top-level `launch_ros` package."""
    mod = types.ModuleType("launch_ros")
    mod.__path__ = []
    mod.__package__ = "launch_ros"
    return mod


def _build_patched_launch_ros_actions():
    mod = types.ModuleType("launch_ros.actions")
    mod.Node = _TrackedNode
    mod.LifecycleNode = _TrackedLifecycleNode
    mod.ComposableNodeContainer = _TrackedComposableNodeContainer
    mod.LoadComposableNodes = _TrackedLoadComposableNodes
    mod.SetParameter = _TrackedSetParameter
    mod.SetRemap = lambda *a, **kw: None
    mod.PushRosNamespace = _TrackedPushRosNamespace
    mod.SetParametersCallback = lambda *a, **kw: None
    return mod


def _build_patched_launch_ros_utilities():
    mod = types.ModuleType("launch_ros.utilities")

    def make_namespace_absolute(namespace):
        """Ensure namespace starts with /."""
        ns = str(namespace) if namespace else ""
        if ns and not ns.startswith("/"):
            return "/" + ns
        return ns or "/"

    def prefix_namespace(namespace, name):
        """Prepend namespace to name."""
        ns = str(namespace).rstrip("/") if namespace else ""
        n = str(name).lstrip("/") if name else ""
        if not ns:
            return n
        if not n:
            return ns
        return ns + "/" + n

    mod.make_namespace_absolute = make_namespace_absolute
    mod.prefix_namespace = prefix_namespace
    mod.get_node_name_count = lambda *a, **kw: 0
    mod.evaluate_parameters = lambda *a, **kw: []
    mod.normalize_parameters = lambda *a, **kw: []
    mod.add_node_name_count_to_name = lambda name, **kw: name
    return mod


def _build_patched_launch_ros_descriptions():
    mod = types.ModuleType("launch_ros.descriptions")
    mod.ComposableNode = _TrackedComposableNode
    mod.ParameterFile = _TrackedParameterFile
    return mod


def _build_patched_launch_substitutions():
    """Always-stub: avoids recursion since we also patch top-level `launch`."""
    mod = types.ModuleType("launch.substitutions")
    mod.__path__ = []  # mark as package so submodule imports work
    mod.FindPackageShare = _TrackedFindPackageShare
    mod.PathJoinSubstitution = _TrackedPathJoinSubstitution
    mod.LaunchConfiguration = _LaunchConfiguration
    _SENTINEL = object()

    class _DeferredEnvironmentVariable:
        """Deferred substitution: reads context.environment at perform() time."""

        def __init__(self, name, **kw):
            self._name = name
            self._default = kw.get("default_value", _SENTINEL)

        def perform(self, context=None):
            name = _to_str(self._name, context) or ""
            # Look up in context environment, then process env.
            if context is not None and hasattr(context, "environment"):
                env = context.environment
                if name in env:
                    return env[name]
            if name in os.environ:
                return os.environ[name]
            # No match — use default if provided, otherwise error.
            if self._default is not _SENTINEL:
                return _to_str(self._default, context) or ""
            logger.error("EnvironmentVariable: '%s' is not set and no default", name)
            return ""

        def __str__(self):
            # Avoid calling perform() without context — return the raw name.
            return str(self._name) if self._name is not None else ""

    mod.EnvironmentVariable = _DeferredEnvironmentVariable
    mod.TextSubstitution = lambda text="", **kw: str(text)
    mod.PythonExpression = lambda expression=None, **kw: None
    return mod


def _build_patched_launch_substitutions_environment_variable():
    """Submodule stub for ``from launch.substitutions.environment_variable import ...``."""
    parent = sys.modules.get("launch.substitutions")
    if parent is None:
        parent = _build_patched_launch_substitutions()
    mod = types.ModuleType("launch.substitutions.environment_variable")
    mod.EnvironmentVariable = parent.EnvironmentVariable
    return mod


def _build_patched_launch_actions():
    """Always-stub: avoids recursion since we also patch top-level `launch`."""
    mod = types.ModuleType("launch.actions")
    mod.IncludeLaunchDescription = _TrackedIncludeLaunchDescription
    mod.DeclareLaunchArgument = _DeclaredArg
    mod.OpaqueFunction = _TrackedOpaqueFunction
    mod.GroupAction = _TrackedGroupAction
    mod.SetLaunchConfiguration = _SetLaunchConfiguration
    mod.LogInfo = lambda *a, **kw: None
    mod.TimerAction = _TimerAction
    mod.RegisterEventHandler = _TrackedRegisterEventHandler
    mod.EmitEvent = _TrackedEmitEvent
    mod.Shutdown = _TrackedShutdown
    mod.PushLaunchConfigurations = lambda *a, **kw: None
    mod.PopLaunchConfigurations = lambda *a, **kw: None
    mod.SetEnvironmentVariable = _TrackedSetEnvironmentVariable
    mod.UnsetEnvironmentVariable = _TrackedUnsetEnvironmentVariable
    mod.ExecuteProcess = _TrackedExecutable
    mod.ExecuteLocal = lambda *a, **kw: None
    mod.OnProcessExit = _TrackedOnProcessExit
    mod.OnProcessStart = _TrackedOnProcessStart
    return mod


def _build_patched_launch_event_handlers():
    """Tracked stubs for launch.event_handlers — record event handler structure."""
    mod = types.ModuleType("launch.event_handlers")
    mod.OnProcessExit = _TrackedOnProcessExit
    mod.OnProcessStart = _TrackedOnProcessStart
    mod.OnProcessIO = lambda *a, **kw: None
    mod.OnShutdown = _TrackedOnShutdown
    mod.OnStateTransition = _TrackedOnStateTransition
    mod.OnExecutionComplete = lambda *a, **kw: None
    return mod


def _build_patched_launch_conditions():
    mod = types.ModuleType("launch.conditions")

    class _IfCondition:
        """Stub for IfCondition that supports .evaluate(context)."""

        def __init__(self, condition=None, **kwargs):
            self._condition = condition

        def evaluate(self, context):
            try:
                if isinstance(self._condition, Substitution):
                    val = str(self._condition.perform(context)).lower()
                    return val in ("true", "1", "yes", "on")
                if self._condition is None:
                    return False
                return bool(self._condition)
            except Exception:
                return False

    class _UnlessCondition(_IfCondition):
        """Stub for UnlessCondition: evaluate() returns the logical inverse of IfCondition."""

        def evaluate(self, context):
            return not super().evaluate(context)

    class _LaunchConfigurationEquals:
        def __init__(self, name=None, expected_value=None, **kwargs):
            self._name = name
            self._expected = expected_value

        def evaluate(self, context):
            try:
                val = context._launch_configurations.get(str(self._name), "")
                return str(val) == str(self._expected)
            except Exception:
                return False

    class _LaunchConfigurationNotEquals(_LaunchConfigurationEquals):
        def evaluate(self, context):
            return not super().evaluate(context)

    mod.IfCondition = _IfCondition
    mod.UnlessCondition = _UnlessCondition
    mod.LaunchConfigurationEquals = _LaunchConfigurationEquals
    mod.LaunchConfigurationNotEquals = _LaunchConfigurationNotEquals
    return mod


def _build_patched_launch_launch_description_sources():
    mod = types.ModuleType("launch.launch_description_sources")

    class _PythonLaunchDescriptionSource:
        """Matches official LaunchDescriptionSource: stores raw location,
        resolves lazily via get_launch_description(context)."""

        def __init__(self, location=None, **kwargs):
            # Store raw location (list of substitutions or string)
            # Matches official: __init__ does NOT resolve substitutions
            if location is None:
                self._location_subs = None
                self._location = None
            elif isinstance(location, str):
                self._location_subs = None
                self._location = location
            else:
                # list of subs or single sub — store for deferred resolution
                self._location_subs = location if isinstance(location, list) else [location]
                self._location = None

        def _resolve_location(self, context):
            """Resolve location substitutions with a real context."""
            if self._location is not None:
                return self._location
            if self._location_subs is None:
                return None
            parts = []
            for sub in self._location_subs:
                if isinstance(sub, Substitution):
                    try:
                        result = sub.perform(context)
                        parts.append(str(result) if result is not None else str(sub))
                    except Exception:
                        parts.append(str(sub))
                else:
                    parts.append(str(sub))
            self._location = "".join(parts)
            return self._location

        @property
        def location(self):
            """Return location string (unresolved display if not yet resolved)."""
            if self._location is not None:
                return self._location
            if self._location_subs is None:
                return None
            # Not yet resolved — return display form (like official ROS 2)
            return " + ".join(str(sub) for sub in self._location_subs)

    class _AnyLaunchDescriptionSource(_PythonLaunchDescriptionSource):
        pass

    mod.PythonLaunchDescriptionSource = _PythonLaunchDescriptionSource
    mod.AnyLaunchDescriptionSource = _AnyLaunchDescriptionSource
    return mod


def _build_patched_launch_ros_substitutions():
    """launch_ros.substitutions — provides FindPackageShare (same as launch.substitutions)."""
    mod = types.ModuleType("launch_ros.substitutions")
    mod.FindPackageShare = _TrackedFindPackageShare
    return mod


def _build_patched_launch_ros_parameter_descriptions():
    mod = types.ModuleType("launch_ros.parameter_descriptions")
    mod.ParameterFile = _TrackedParameterFile
    mod.ParameterDescription = lambda *a, **kw: None
    mod.ParameterValue = lambda *a, **kw: None
    return mod


def _build_patched_ament_index_python():
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
        # Return the parent of the share directory as a best-effort prefix.
        # For unresolved packages the share path is portable syntax like
        # "$(find-pkg-share pkg)" — fall back to the ROS distro prefix.
        share = _R.get_state().resolve_pkg_share(package_name)
        if share.startswith("$("):
            return _ROS_DISTRO_PREFIX
        p = Path(share)
        return str(p.parent) if p.parent != p else _ROS_DISTRO_PREFIX

    mod.get_package_share_directory = get_package_share_directory
    mod.get_package_prefix = get_package_prefix
    return mod


def _build_patched_launch_ros_events():
    """Patched launch_ros.events — provides ChangeState."""
    mod = types.ModuleType("launch_ros.events")
    mod.__path__ = []
    mod.__package__ = "launch_ros.events"
    mod.ChangeState = _TrackedChangeState
    return mod


def _build_patched_launch_ros_events_lifecycle():
    """Patched launch_ros.events.lifecycle — provides ChangeState."""
    mod = types.ModuleType("launch_ros.events.lifecycle")
    mod.ChangeState = _TrackedChangeState
    return mod


def _build_patched_launch_ros_event_handlers():
    """Patched launch_ros.event_handlers — lifecycle event matchers."""
    mod = types.ModuleType("launch_ros.event_handlers")
    mod.__path__ = []
    mod.__package__ = "launch_ros.event_handlers"
    mod.OnStateTransition = _TrackedOnStateTransition
    return mod


def _build_patched_launch_ros_event_handlers_on_state_transition():
    mod = types.ModuleType("launch_ros.event_handlers.on_state_transition")
    mod.OnStateTransition = _TrackedOnStateTransition
    return mod


def _build_patched_lifecycle_msgs():
    """Patched lifecycle_msgs — provides Transition constants."""
    mod = types.ModuleType("lifecycle_msgs")
    mod.__path__ = []
    mod.__package__ = "lifecycle_msgs"
    return mod


def _build_patched_lifecycle_msgs_msg():
    """Patched lifecycle_msgs.msg — provides Transition constants."""
    mod = types.ModuleType("lifecycle_msgs.msg")

    class Transition:
        TRANSITION_CONFIGURE = 1
        TRANSITION_CLEANUP = 2
        TRANSITION_ACTIVATE = 3
        TRANSITION_DEACTIVATE = 4
        TRANSITION_UNCONFIGURED_SHUTDOWN = 5
        TRANSITION_INACTIVE_SHUTDOWN = 6
        TRANSITION_ACTIVE_SHUTDOWN = 7

    mod.Transition = Transition
    return mod


def _build_patched_launch_events():
    """Patched launch.events — provides Shutdown event and matches_action."""
    mod = types.ModuleType("launch.events")
    mod.__path__ = []
    mod.__package__ = "launch.events"
    mod.Shutdown = _TrackedShutdown
    mod.matches_action = _TrackedMatchesAction
    return mod


def _build_patched_launch_events_process():
    mod = types.ModuleType("launch.events.process")
    return mod


def _build_patched_launch_event_handler():
    mod = types.ModuleType("launch.event_handler")
    mod.EventHandler = lambda *a, **kw: None
    return mod


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Intercept imports of specific launch/ament modules and return patched versions."""

    PATCHED = {
        "launch": _build_patched_launch,
        "launch_ros": _build_patched_launch_ros,
        "launch_ros.actions": _build_patched_launch_ros_actions,
        "launch_ros.descriptions": _build_patched_launch_ros_descriptions,
        "launch_ros.substitutions": _build_patched_launch_ros_substitutions,
        "launch_ros.parameter_descriptions": _build_patched_launch_ros_parameter_descriptions,
        "launch_ros.utilities": _build_patched_launch_ros_utilities,
        "launch.substitutions": _build_patched_launch_substitutions,
        "launch.substitutions.environment_variable": (
            _build_patched_launch_substitutions_environment_variable
        ),
        "launch.actions": _build_patched_launch_actions,
        "launch.event_handlers": _build_patched_launch_event_handlers,
        "launch.conditions": _build_patched_launch_conditions,
        "launch.launch_description_sources": _build_patched_launch_launch_description_sources,
        "ament_index_python": _build_patched_ament_index_python,
        "ament_index_python.packages": _build_patched_ament_index_python_packages,
        "launch_ros.events": _build_patched_launch_ros_events,
        "launch_ros.events.lifecycle": _build_patched_launch_ros_events_lifecycle,
        "launch_ros.event_handlers": _build_patched_launch_ros_event_handlers,
        "launch_ros.event_handlers.on_state_transition": (
            _build_patched_launch_ros_event_handlers_on_state_transition
        ),
        "lifecycle_msgs": _build_patched_lifecycle_msgs,
        "lifecycle_msgs.msg": _build_patched_lifecycle_msgs_msg,
        "launch.events": _build_patched_launch_events,
        "launch.events.process": _build_patched_launch_events_process,
        "launch.event_handler": _build_patched_launch_event_handler,
    }

    def find_spec(self, fullname, path, target=None):
        if fullname in self.PATCHED:
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        if spec.name not in _PATCHED_MODULES:
            _PATCHED_MODULES[spec.name] = self.PATCHED[spec.name]()
        return _PATCHED_MODULES[spec.name]

    def exec_module(self, module):
        pass  # Already fully initialised in create_module
