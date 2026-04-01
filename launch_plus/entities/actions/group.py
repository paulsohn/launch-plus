"""Action handler for <group> element."""

from __future__ import annotations

import logging

import launch_plus.resolver as _R
from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity

logger = logging.getLogger("launch_plus")


@expose_action("group")
class GroupAction(Action):
    """Groups child actions with optional scoped launch_configurations and environment.

    Matching official ``GroupAction``: push/pop launch_configurations and
    environment when ``scoped=True``.
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
            scoped=scoped,
            _xml_children=list(entity.children),
            _xml_include_stack=list(parser.include_stack),
        )

    def __init__(self, actions=None, **kwargs):
        self._actions = list(actions or [])
        self._scoped = kwargs.get("scoped", True)
        self._condition = kwargs.get("condition")
        # XML path: child entities to parse at execute time
        self._xml_children = kwargs.get("_xml_children")
        self._xml_include_stack = kwargs.get("_xml_include_stack")

    def execute(self, context) -> list:
        """Execute group: push/pop scope around children.

        Handles both XML children (Entity objects parsed at execute time)
        and shim children (Action objects from Python import-patching).
        """
        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return []
            except Exception as e:
                logger.warning("GroupAction condition evaluation failed: %s", e)
                return []

        if self._scoped:
            context._push_launch_configurations()
            context._push_environment()
        try:
            # XML path: parse and execute child entities
            if self._xml_children:
                for child in self._xml_children:
                    _R._resolve_element(child, context, self._xml_include_stack or [])

            # Shim path: walk child actions
            if self._actions:
                _R._walk_actions(context._state, self._actions, context)
        finally:
            if self._scoped:
                context._pop_environment()
                context._pop_launch_configurations()

        return []


class OpaqueFunction(Action):
    """Stores an OpaqueFunction's callable so the walker can invoke it.

    Walks returned actions internally rather than deferring to the parent walker.
    """

    def __init__(self, *, function=None, **kwargs):
        self.function = function

    def execute(self, context) -> list:
        fn = self.function
        if fn and context:
            state = context._state
            try:
                result = _R._call_opaque_with_stubs(state, fn, context)
                if result:
                    _R._walk_actions(state, result, context)
            except Exception as e:
                logger.error("OpaqueFunction failed: %s", e)
        return []


class TimerAction(Action):
    """TimerAction cannot be statically resolved.

    Timer-based actions depend on runtime timing which is not sequential
    and cannot be determined during static analysis.
    """

    def __init__(self, *, period=None, actions=None, **kwargs):
        self._actions = list(actions or [])

    def execute(self, context) -> list:
        logger.error(
            "TimerAction cannot be statically resolved: context execution timing is not sequential"
        )
        return []
