"""Action handlers for environment and configuration elements.

Covers: <set_env>, <unset_env>, <push-ros-namespace>,
<set_parameter>, <set_remap>.
"""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
)


@expose_action("set_env")
def _action_set_env(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = parser.resolve(entity.get_attr("name", optional=True) or "")
        value = parser.resolve(entity.get_attr("value", optional=True) or "")
        _state.env[name] = value
        parser.ctx.env[name] = value


@expose_action("unset_env")
def _action_unset_env(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        name = parser.resolve(entity.get_attr("name", optional=True) or "")
        _state.env.pop(name, None)
        parser.ctx.env.pop(name, None)


@expose_action("push-ros-namespace")
def _action_push_ros_namespace(entity: Entity, parser: _ActionParser) -> None:
    if parser.evaluate_condition(entity):
        ns = parser.resolve(entity.get_attr("namespace", optional=True) or "")
        if ns:
            _state.namespace_stack.append(ns)


@expose_action("set_parameter")
def _action_set_parameter(entity: Entity, parser: _ActionParser) -> None:
    name = parser.resolve(entity.get_attr("name", optional=True) or "")
    value = parser.resolve(entity.get_attr("value", optional=True) or "")
    _state.tracked["global_params"].append([name, value])
    _state.global_params.append((name, value))


@expose_action("set_remap")
def _action_set_remap(entity: Entity, parser: _ActionParser) -> None:
    src = parser.resolve(entity.get_attr("from", optional=True) or "")
    dst = parser.resolve(entity.get_attr("to", optional=True) or "")
    _state.global_remaps.append((src, dst))


# ─── Python-shim actions ─────────────────────────────────────────────────────

import os  # noqa: E402

import launch_plus.resolver as _R  # noqa: E402
from launch_plus.entities.actions.base import _TrackedAction  # noqa: E402
from launch_plus.entities.state import (  # noqa: E402
    _error,
    _PackageNotFetchedError,
    _warn,
)


class _TrackedPushRosNamespace(_TrackedAction):
    """Tracks PushRosNamespace so execute() can update _state.namespace_stack."""

    def __init__(self, namespace=None, **kwargs):
        self._namespace = namespace

    def execute(self, context) -> list | None:
        if self._namespace is not None and context is not None:
            ns = _R._resolve_substitution(self._namespace, context)
            if ns:
                _R._state.namespace_stack.append(ns)
        return None


class _TrackedSetEnvironmentVariable(_TrackedAction):
    """Tracks SetEnvironmentVariable: mutates _state.env."""

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value
        self._condition = kwargs.get("condition")

    def execute(self, context) -> list | None:
        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"SetEnvironmentVariable condition evaluation failed: {e}")
                return None
        name = _R._to_str(self._name, context)
        if not name:
            _error("SetEnvironmentVariable: resolved name is empty or None — skipping")
            return None
        value = _R._to_str(self._value, context) or ""
        _R._state.env[name] = value
        return None


class _TrackedUnsetEnvironmentVariable(_TrackedAction):
    """Tracks UnsetEnvironmentVariable: removes from _state.env."""

    def __init__(self, name=None, **kwargs):
        self._name = name
        self._condition = kwargs.get("condition")

    def execute(self, context) -> list | None:
        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"UnsetEnvironmentVariable condition evaluation failed: {e}")
                return None
        name = _R._to_str(self._name, context)
        if not name:
            _error("UnsetEnvironmentVariable: resolved name is empty or None — skipping")
            return None
        if name in os.environ:
            _error(
                f"unset_env: '{name}' exists in the process env and cannot be unset. "
                f'Use SetEnvironmentVariable(name="{name}", value="") '
                "or a scoped group instead"
            )
        elif name in _R._state.env:
            del _R._state.env[name]
        else:
            _error(f"unset_env: environment variable '{name}' is not set")
        return None
