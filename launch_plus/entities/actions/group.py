"""Action handler for <group> element."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import launch_plus.resolver as _R
from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity

logger = logging.getLogger("launch_plus")


@expose_action("group")
class GroupAction(Action):
    """Groups child actions with optional scoped launch_configurations and environment.

    Children can be Entity objects (from XML parse), Action objects (from Python
    shim), or real ROS 2 objects (from OpaqueFunction). All are handled uniformly
    at execute time.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        scoped_raw = entity.get_attr("scoped", optional=True)
        if scoped_raw is None:
            scoped = True
        elif isinstance(scoped_raw, bool):
            scoped = scoped_raw
        else:
            scoped = str(scoped_raw).lower() not in ("false", "0", "no")
        return cls(
            actions=list(entity.children),
            scoped=scoped,
            _include_stack=list(parser.include_stack),
        )

    def __init__(self, actions=None, **kwargs):
        self.actions: list = list(actions or [])
        self.scoped: bool = kwargs.get("scoped", True)
        self.condition = kwargs.get("condition")
        self._include_stack: list = kwargs.get("_include_stack", [])
        # Set on resolved GroupAction instances (from IncludeLaunchDescription)
        self.resolved_children: list = kwargs.get("resolved_children", [])

    def execute(self, context) -> list:
        """Execute group: push/pop scope, execute children, return results."""
        if self.condition is not None and context is not None:
            try:
                if not self.condition.evaluate(context):
                    return []
            except Exception as e:
                logger.warning("GroupAction condition evaluation failed: %s", e)
                return []

        if self.scoped:
            context._push_launch_configurations()
            context._push_environment()
        results: list = []
        try:
            for child in self.actions:
                if child is None:
                    continue
                if isinstance(child, Entity):
                    results.extend(_R._resolve_element(child, context, self._include_stack))
                elif isinstance(child, Action):
                    children = child.execute(context)
                    if children:
                        results.extend(children)
                else:
                    logger.error("expected Action or Entity, got %s", type(child).__name__)
        finally:
            if self.scoped:
                context._pop_environment()
                context._pop_launch_configurations()

        return results

    def serialize_resolved(self) -> list[ET.Element]:
        """Render as <group> with children serialized recursively."""
        if not self.resolved_children:
            return []
        group = ET.Element("group")
        for child in self.resolved_children:
            for elem in child.serialize_resolved():
                group.append(elem)
        return [group]


class OpaqueFunction(Action):
    """Stores an OpaqueFunction's callable so the walker can invoke it."""

    def __init__(self, *, function=None, **kwargs):
        self.function = function

    def execute(self, context) -> list:
        from launch_plus.entities.opaque_stubs import _call_opaque_with_stubs

        fn = self.function
        if fn and context:
            try:
                result = _call_opaque_with_stubs(context._state, fn, context)
                if result:
                    return _R._execute_actions(result, context)
            except Exception as e:
                logger.error("OpaqueFunction failed: %s", e)
        return []


class TimerAction(Action):
    """TimerAction cannot be statically resolved.

    Timer-based actions depend on runtime timing which is not sequential
    and cannot be determined during static analysis.
    """

    def __init__(self, *, period=None, actions=None, **kwargs):
        self.actions: list = list(actions or [])

    def execute(self, context) -> list:
        logger.error(
            "TimerAction cannot be statically resolved: context execution timing is not sequential"
        )
        return []
