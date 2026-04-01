"""Action handlers for event-related elements.

Covers: <on_process_start>, <on_process_exit>, <on_state_transition>,
<on_shutdown>, <emit_event>.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("on_process_start")
@expose_action("on_process_exit")
@expose_action("on_state_transition")
@expose_action("on_shutdown")
class EventHandler(Action):
    """Tracks <on_process_start>, <on_process_exit>, <on_state_transition>, <on_shutdown>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        handler_kind = entity.type_name
        target = entity.get_attr("target", optional=True)
        target_node = entity.get_attr("target_node", optional=True)
        handler_ns = entity.get_attr("namespace", optional=True)
        start_state = entity.get_attr("start_state", optional=True)
        goal_state = entity.get_attr("goal_state", optional=True)
        # Collect child emit_event entities as raw attribute dicts
        child_events = []
        for child in entity.children:
            if child.type_name == "emit_event":
                child_events.append(
                    {
                        "event": child.get_attr("event", optional=True) or "",
                        "target_node": child.get_attr("target_node", optional=True),
                        "namespace": child.get_attr("namespace", optional=True),
                    }
                )
        return cls(
            handler_kind=handler_kind,
            target=target,
            target_node=target_node,
            handler_ns=handler_ns,
            start_state=start_state,
            goal_state=goal_state,
            child_events=child_events,
        )

    def __init__(
        self,
        *,
        handler_kind="",
        target=None,
        target_node=None,
        handler_ns=None,
        start_state=None,
        goal_state=None,
        child_events=None,
        **kwargs,
    ):
        self._handler_kind = handler_kind
        self._target = target
        self._target_node = target_node
        self._handler_ns = handler_ns
        self._start_state = start_state
        self._goal_state = goal_state
        self._child_events = child_events or []
        self._resolved_handler_kind: str = handler_kind or ""
        self._resolved_target: str | None = None
        self._resolved_target_node: str | None = None
        self._resolved_start_state: str | None = None
        self._resolved_goal_state: str | None = None
        self._resolved_namespace: str | None = None
        self._resolved_actions: list = []

    def execute(self, context) -> list:
        from launch_plus.entities.helpers import resolve_value

        eh_actions: list[dict] = []
        for ce in self._child_events:
            eh_actions.append(
                {
                    "event": resolve_value(ce["event"], context) or "",
                    "target_node": resolve_value(ce["target_node"], context),
                    "namespace_stack": [],
                    "explicit_namespace": resolve_value(ce["namespace"], context),
                }
            )

        resolved = EventHandler(handler_kind=self._handler_kind)
        resolved._resolved_handler_kind = self._handler_kind
        resolved._resolved_target = resolve_value(self._target, context)
        resolved._resolved_target_node = resolve_value(self._target_node, context)
        resolved._resolved_start_state = resolve_value(self._start_state, context)
        resolved._resolved_goal_state = resolve_value(self._goal_state, context)
        resolved._resolved_namespace = resolve_value(self._handler_ns, context)
        resolved._resolved_actions = eh_actions
        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        kind = getattr(self, "_resolved_handler_kind", None)
        if kind is None:
            return []

        elem = ET.Element(kind)
        if self._resolved_target is not None:
            elem.set("target", self._resolved_target)
        if self._resolved_target_node is not None:
            elem.set("target_node", self._resolved_target_node)
        if self._resolved_namespace is not None:
            elem.set("namespace", self._resolved_namespace)
        if self._resolved_start_state is not None:
            elem.set("start_state", self._resolved_start_state)
        if self._resolved_goal_state is not None:
            elem.set("goal_state", self._resolved_goal_state)

        for a in getattr(self, "_resolved_actions", []):
            emit = ET.SubElement(elem, "emit_event")
            emit.set("event", a.get("event", ""))
            tn = a.get("target_node")
            if tn:
                emit.set("target_node", tn)
            ns = a.get("explicit_namespace")
            if ns:
                emit.set("namespace", ns)

        return [elem]


@expose_action("emit_event")
class EmitEvent(Action):
    """Tracks <emit_event> — records an event emission."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        event = entity.get_attr("event", optional=True) or ""
        target_node = entity.get_attr("target_node", optional=True)
        ee_ns = entity.get_attr("namespace", optional=True)
        return cls(event=event, target_node=target_node, namespace=ee_ns)

    def __init__(self, *, event="", target_node=None, namespace=None, **kwargs):
        self._event = event
        self._target_node_attr = target_node
        self._namespace = namespace
        self._resolved_event: str = ""
        self._resolved_target_node: str | None = None
        self._resolved_namespace: str | None = None

    def execute(self, context) -> list:
        from launch_plus.entities.helpers import resolve_value

        event = resolve_value(self._event, context) or ""
        target_node = resolve_value(self._target_node_attr, context)
        ee_ns = resolve_value(self._namespace, context)

        resolved = EmitEvent(event=event, target_node=target_node, namespace=ee_ns)
        resolved._resolved_event = event
        resolved._resolved_target_node = target_node
        resolved._resolved_namespace = ee_ns
        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        event = getattr(self, "_resolved_event", None)
        if event is None:
            return []
        elem = ET.Element("emit_event")
        elem.set("event", event)
        tn = getattr(self, "_resolved_target_node", None)
        if tn:
            elem.set("target_node", tn)
        ns = getattr(self, "_resolved_namespace", None)
        if ns:
            elem.set("namespace", ns)
        return [elem]


# ─── Python-shim actions ─────────────────────────────────────────────────────

from launch_plus.entities.action import Action  # noqa: E402


class TrackedEmitEvent(Action):
    """Tracked emit_event — Python shim for import patching."""

    def __init__(self, event=None, **kwargs):
        self._event = event


class ChangeState(Action):
    """Tracked ChangeState — Python shim for import patching."""

    def __init__(self, lifecycle_node_matcher=None, transition_id=None, **kwargs):
        pass


class Shutdown(Action):
    """Tracked Shutdown — Python shim for import patching."""

    def __init__(self, **kwargs):
        pass


class OnProcessStart(Action):
    """Tracked OnProcessStart — Python shim for import patching."""

    def __init__(self, target_action=None, on_start=None, **kwargs):
        self._target_action = target_action
        self._actions = on_start or []


class OnProcessExit(Action):
    """Tracked OnProcessExit — Python shim for import patching."""

    def __init__(self, target_action=None, on_exit=None, **kwargs):
        self._target_action = target_action
        self._actions = on_exit or []


class OnStateTransition(Action):
    """Tracked OnStateTransition — Python shim for import patching."""

    def __init__(
        self, target_lifecycle_node=None, start_state=None, goal_state=None, entities=None, **kwargs
    ):
        self._target_lifecycle_node = target_lifecycle_node
        self._actions = entities or []


class OnShutdown(Action):
    """Tracked OnShutdown — Python shim for import patching."""

    def __init__(self, on_shutdown=None, **kwargs):
        self._actions = on_shutdown or []


class RegisterEventHandler(Action):
    """Tracked RegisterEventHandler — Python shim for import patching."""

    def __init__(self, event_handler=None, **kwargs):
        self._event_handler = event_handler
