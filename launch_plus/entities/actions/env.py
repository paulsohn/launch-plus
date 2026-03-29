"""Action handlers for environment and configuration elements.

Covers: <set_env>, <unset_env>, <push-ros-namespace>,
<set_parameter>, <set_remap>.
"""

from __future__ import annotations

import os

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.state import (
    _error,
    _warn,
)
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("set_env")
class _TrackedSetEnvironmentVariable(_TrackedAction):
    """Tracks SetEnvironmentVariable / <set_env>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        name = parser.parse_substitution(entity.get_attr("name", optional=True) or "")
        value = parser.parse_substitution(entity.get_attr("value", optional=True) or "")
        return cls(name=name, value=value)

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value
        self._condition = kwargs.get("condition")

    def execute(self, context) -> list | None:
        from launch_plus.entities.xml_resolver import resolve_value

        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except Exception as e:
                _warn(f"SetEnvironmentVariable condition evaluation failed: {e}")
                return None
        name = resolve_value(self._name, context)
        if not name:
            _error("SetEnvironmentVariable: resolved name is empty or None — skipping")
            return None
        value = resolve_value(self._value, context) or ""
        _R._state.env[name] = value
        return None


@expose_action("unset_env")
class _TrackedUnsetEnvironmentVariable(_TrackedAction):
    """Tracks UnsetEnvironmentVariable / <unset_env>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        name = parser.parse_substitution(entity.get_attr("name", optional=True) or "")
        return cls(name=name)

    def __init__(self, name=None, **kwargs):
        self._name = name
        self._condition = kwargs.get("condition")

    def execute(self, context) -> list | None:
        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except Exception as e:
                _warn(f"UnsetEnvironmentVariable condition evaluation failed: {e}")
                return None
        from launch_plus.entities.xml_resolver import resolve_value

        name = resolve_value(self._name, context)
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


@expose_action("push-ros-namespace")
class _TrackedPushRosNamespace(_TrackedAction):
    """Tracks PushRosNamespace / <push-ros-namespace>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        ns_tokens = parser.parse_substitution(entity.get_attr("namespace", optional=True) or "")
        return cls(namespace=ns_tokens)

    def __init__(self, namespace=None, **kwargs):
        self._namespace = namespace

    def execute(self, context) -> list | None:
        from launch_plus.entities.xml_resolver import resolve_value

        ns = resolve_value(self._namespace, context)
        if ns:
            _R._state.namespace_stack.append(ns)
        return None


@expose_action("set_remap")
class _SetRemap(_TrackedAction):
    """Tracks <set_remap> — records a global remap."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        src = parser.parse_substitution(entity.get_attr("from", optional=True) or "")
        dst = parser.parse_substitution(entity.get_attr("to", optional=True) or "")
        return cls(src=src, dst=dst)

    def __init__(self, src="", dst="", **kwargs):
        self._src = src
        self._dst = dst

    def execute(self, context) -> list | None:
        from launch_plus.entities.xml_resolver import resolve_value

        src = resolve_value(self._src, context) or ""
        dst = resolve_value(self._dst, context) or ""
        _R._state.global_remaps.append((src, dst))
        return None
