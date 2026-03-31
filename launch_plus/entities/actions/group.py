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
    """Stores GroupAction's child actions so the walker can recurse into them."""

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
            _xml_ctx=parser.ctx,
        )

    def __init__(self, actions=None, **kwargs):
        self._actions = list(actions or [])
        self._scoped = kwargs.get("scoped", True)
        self._condition = kwargs.get("condition")
        # XML path: child entities to resolve in execute()
        self._xml_children = kwargs.get("_xml_children")
        self._xml_include_stack = kwargs.get("_xml_include_stack")
        self._xml_ctx = kwargs.get("_xml_ctx")

    def execute(self, context) -> list | None:
        if self._condition is not None and context is not None:
            try:
                if not self._condition.evaluate(context):
                    return None
            except Exception as e:
                logger.warning("GroupAction condition evaluation failed: %s", e)
                return None
        if self._xml_children is not None:
            self._execute_xml(context)
        else:
            self._execute_shim(context)
        return None

    def _execute_xml(self, ctx) -> None:
        """Execute for XML path: resolve child entities with scoping.

        Matching official GroupAction: push/pop launch_configurations and
        environment when scoped=True.
        """
        from launch_plus.resolver import _resolve_element

        if self._scoped:
            ctx._push_launch_configurations()
            ctx._push_environment()
        for child in self._xml_children:
            _resolve_element(child, ctx, self._xml_include_stack or [])
        if self._scoped:
            ctx._pop_environment()
            ctx._pop_launch_configurations()

    def _execute_shim(self, context) -> None:
        """Execute for Python shim path: walk child actions.

        Matching official GroupAction: push/pop launch_configurations and
        environment when scoped=True.
        """
        if self._scoped:
            context._push_launch_configurations()
            context._push_environment()
        _R._walk_actions(context._state, self._actions, context)
        if self._scoped:
            context._pop_environment()
            context._pop_launch_configurations()


class OpaqueFunction(Action):
    """Stores an OpaqueFunction's callable so the walker can invoke it."""

    def __init__(self, *, function=None, **kwargs):
        self.function = function

    def execute(self, context) -> list | None:
        fn = self.function
        if fn and context:
            state = context._state
            try:
                result = _R._call_opaque_with_stubs(state, fn, context)
                return result if result else None
            except Exception as e:
                logger.error("OpaqueFunction failed: %s", e)
        return None


class TimerAction(Action):
    """Stores TimerAction child actions so the walker can recurse into them."""

    def __init__(self, *, period=None, actions=None, **kwargs):
        self._actions = list(actions or [])

    def execute(self, context) -> list | None:
        return self._actions or None
