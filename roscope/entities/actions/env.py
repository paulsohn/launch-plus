"""Action handlers for environment and configuration elements.

Covers: <set_env>, <unset_env>, <push-ros-namespace>, <set_remap>.
"""

from __future__ import annotations

import logging
import os

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("set_env")
class SetEnvironmentVariable(Action):
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
        from roscope.entities.helpers import resolve_value

        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except Exception as e:
                logger.warning("SetEnvironmentVariable condition evaluation failed: %s", e)
                return None
        name = resolve_value(self._name, context)
        if not name:
            logger.error("SetEnvironmentVariable: resolved name is empty or None — skipping")
            return None
        value = resolve_value(self._value, context) or ""
        context.environment[name] = value
        return None


@expose_action("unset_env")
class UnsetEnvironmentVariable(Action):
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
                logger.warning("UnsetEnvironmentVariable condition evaluation failed: %s", e)
                return None
        from roscope.entities.helpers import resolve_value

        name = resolve_value(self._name, context)
        if not name:
            logger.error("UnsetEnvironmentVariable: resolved name is empty or None — skipping")
            return None
        if name in os.environ:
            logger.error(
                "unset_env: '%s' exists in the process env and cannot be unset. "
                'Use SetEnvironmentVariable(name="%s", value="") '
                "or a scoped group instead",
                name,
                name,
            )
        elif name in context.environment:
            del context.environment[name]
        else:
            logger.error("unset_env: environment variable '%s' is not set", name)
        return None


@expose_action("push-ros-namespace")
class PushRosNamespace(Action):
    """Tracks PushRosNamespace / <push-ros-namespace>.

    Matching official: computes cumulative namespace and stores in
    ``context.launch_configurations['ros_namespace']``.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        ns_tokens = parser.parse_substitution(entity.get_attr("namespace", optional=True) or "")
        return cls(namespace=ns_tokens)

    def __init__(self, namespace=None, **kwargs):
        self._namespace = namespace

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import _ros2_namespace_join, resolve_value

        ns = resolve_value(self._namespace, context)
        if ns:
            prev = context._launch_configurations.get("ros_namespace")
            context._launch_configurations["ros_namespace"] = _ros2_namespace_join(prev, ns)
        return None


@expose_action("set_remap")
class SetRemap(Action):
    """Tracks SetRemap / <set_remap>.

    Matching official: appends to ``context.launch_configurations['ros_remaps']``.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        src = parser.parse_substitution(entity.get_attr("from", optional=True) or "")
        dst = parser.parse_substitution(entity.get_attr("to", optional=True) or "")
        return cls(src=src, dst=dst)

    def __init__(self, src="", dst="", **kwargs):
        self._src = src
        self._dst = dst

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import resolve_value

        src = resolve_value(self._src, context) or ""
        dst = resolve_value(self._dst, context) or ""
        remaps = context._launch_configurations.get("ros_remaps", [])
        remaps.append((src, dst))
        context._launch_configurations["ros_remaps"] = remaps
        return None
