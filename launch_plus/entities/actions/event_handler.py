"""Action handlers for event-related elements.

Covers: <on_process_start>, <on_process_exit>, <on_state_transition>,
<on_shutdown>, <emit_event>.
"""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
    _track_event_handler,
)


@expose_action("on_process_start")
@expose_action("on_process_exit")
@expose_action("on_state_transition")
@expose_action("on_shutdown")
def _action_event_handler(entity: Entity, parser: _ActionParser) -> None:
    handler_kind = entity.type_name
    target = parser.resolve_optional(entity.get_attr("target", optional=True))
    target_node = parser.resolve_optional(entity.get_attr("target_node", optional=True))
    handler_ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    start_state = parser.resolve_optional(entity.get_attr("start_state", optional=True))
    goal_state = parser.resolve_optional(entity.get_attr("goal_state", optional=True))
    eh_actions: list[dict] = []
    for child in entity.children:
        if child.type_name == "emit_event":
            event = parser.resolve(child.get_attr("event", optional=True) or "")
            ee_target = parser.resolve_optional(child.get_attr("target_node", optional=True))
            ee_ns = parser.resolve_optional(child.get_attr("namespace", optional=True))
            eh_actions.append(
                {
                    "event": event,
                    "target_node": ee_target,
                    "namespace_stack": [],
                    "explicit_namespace": ee_ns,
                }
            )
    _track_event_handler(
        {
            "handler_kind": handler_kind,
            "target": target,
            "target_node": target_node,
            "start_state": start_state,
            "goal_state": goal_state,
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": handler_ns,
            "actions": eh_actions,
        }
    )


@expose_action("emit_event")
def _action_emit_event(entity: Entity, parser: _ActionParser) -> None:
    event = parser.resolve(entity.get_attr("event", optional=True) or "")
    target_node = parser.resolve_optional(entity.get_attr("target_node", optional=True))
    ee_ns = parser.resolve_optional(entity.get_attr("namespace", optional=True))
    _track_event_handler(
        {
            "handler_kind": "emit_event",
            "target": None,
            "target_node": target_node,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": ee_ns,
            "actions": [
                {
                    "event": event,
                    "target_node": target_node,
                    "namespace_stack": list(_state.namespace_stack),
                    "explicit_namespace": ee_ns,
                }
            ],
        }
    )
