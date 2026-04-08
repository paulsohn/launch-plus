"""Action handler for <group> element."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("group")
class GroupAction(Action):
    """Groups child actions with optional scoped launch_configurations and environment.

    Children can be Entity objects (from XML parse), Action objects (from Python
    shim), or real ROS 2 objects (from OpaqueFunction). All are handled uniformly
    at execute time.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        scoped_raw = entity.get_attr("scoped", optional=True)
        if scoped_raw is None:
            kwargs["scoped"] = True
        elif isinstance(scoped_raw, bool):
            kwargs["scoped"] = scoped_raw
        else:
            kwargs["scoped"] = str(scoped_raw).lower() not in ("false", "0", "no")
        kwargs["actions"] = list(entity.children)
        kwargs["_include_stack"] = list(parser.include_stack)
        return cls, kwargs

    def __init__(self, actions=None, **kwargs):
        super().__init__(**kwargs)
        self.actions: list = list(actions or [])
        self.scoped: bool = kwargs.get("scoped", True)
        self._include_stack: list = kwargs.get("_include_stack", [])
        # Set on resolved GroupAction instances (from IncludeLaunchDescription)
        self.resolved_children: list = kwargs.get("resolved_children", [])

    def execute(self, context) -> list:
        """Execute group: push/pop scope, execute children, return results."""

        if self.scoped:
            context._push_launch_configurations()
            context._push_environment()
        results: list = []
        try:
            for child in self.actions:
                if child is None:
                    continue
                if isinstance(child, Entity):
                    from roscope.resolver import _resolve_element

                    results.extend(_resolve_element(child, context, self._include_stack))
                elif isinstance(child, Action):
                    children = child.visit(context)
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
        super().__init__(**kwargs)
        self.function = function

    def execute(self, context) -> list:
        fn = self.function
        if fn and context:
            try:
                result = fn(context)
                if result:
                    from roscope.resolver import _execute_actions

                    return _execute_actions(result, context)
            except Exception as e:
                logger.error("OpaqueFunction failed: %s", e)
        return []


class TimerAction(Action):
    """TimerAction cannot be statically resolved.

    Timer-based actions depend on runtime timing which is not sequential
    and cannot be determined during static analysis.
    """

    def __init__(self, *, period=None, actions=None, **kwargs):
        super().__init__(**kwargs)
        self.actions: list = list(actions or [])

    def execute(self, context) -> list:
        logger.error(
            "TimerAction cannot be statically resolved: context execution timing is not sequential"
        )
        return []
