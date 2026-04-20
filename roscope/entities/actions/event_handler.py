"""Action handlers for event-related elements.

Covers: <on_process_start>, <on_process_exit>, <on_state_transition>,
<on_shutdown>, <emit_event>.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


class EventHandler(Action):
    """Base for event handler actions.

    Subclasses handle both XML parse path (via ``@expose_action`` + ``parse()``)
    and Python shim path (via import patching with compatible ``__init__``).

    Note: event handler actions exist only in Python launch files officially;
    the XML parse path here is ad-hoc (no official XML counterpart).
    """

    def __init__(
        self,
        *,
        handler_kind: str = "",
        target: str | None = None,
        target_node: str | None = None,
        namespace: str | None = None,
        start_state: str | None = None,
        goal_state: str | None = None,
        child_events: list | None = None,
        # Python shim kwargs (accepted but unused for base handler)
        target_action=None,
        on_start=None,
        on_exit=None,
        on_shutdown=None,
        target_lifecycle_node=None,
        entities=None,
        event_handler=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.handler_kind = handler_kind
        self.target = target
        self.target_node = target_node
        self.namespace = namespace
        self.start_state = start_state
        self.goal_state = goal_state
        self.child_events: list = child_events or []
        self.actions: list = []

    @classmethod
    def _parse_xml(cls, entity: Entity, parser: _ActionParser):
        """Shared XML parse logic for all event handler types.

        Ad-hoc — event handlers have no official XML counterpart.
        """
        handler_kind = entity.type_name
        target = entity.get_attr("target", optional=True)
        target_node = entity.get_attr("target_node", optional=True)
        handler_ns = entity.get_attr("namespace", optional=True)
        start_state = entity.get_attr("start_state", optional=True)
        goal_state = entity.get_attr("goal_state", optional=True)
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
            namespace=handler_ns,
            start_state=start_state,
            goal_state=goal_state,
            child_events=child_events,
        )

    def execute(self, context) -> list:
        from roscope.entities.helpers import resolve_value

        child_actions: list[dict] = []
        for ce in self.child_events:
            child_actions.append(
                {
                    "event": resolve_value(ce["event"], context) or "",
                    "target_node": resolve_value(ce["target_node"], context),
                    "namespace": resolve_value(ce["namespace"], context),
                }
            )

        resolved = EventHandler(handler_kind=self.handler_kind)
        resolved.target = resolve_value(self.target, context)
        resolved.target_node = resolve_value(self.target_node, context)
        resolved.start_state = resolve_value(self.start_state, context)
        resolved.goal_state = resolve_value(self.goal_state, context)
        resolved.namespace = resolve_value(self.namespace, context)
        resolved.actions = child_actions
        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.handler_kind:
            return []

        elem = ET.Element(self.handler_kind)
        if self.target is not None:
            elem.set("target", self.target)
        if self.target_node is not None:
            elem.set("target_node", self.target_node)
        if self.namespace is not None:
            elem.set("namespace", self.namespace)
        if self.start_state is not None:
            elem.set("start_state", self.start_state)
        if self.goal_state is not None:
            elem.set("goal_state", self.goal_state)

        for a in self.actions:
            emit = ET.SubElement(elem, "emit_event")
            emit.set("event", a.get("event", ""))
            tn = a.get("target_node")
            if tn:
                emit.set("target_node", tn)
            ns = a.get("namespace")
            if ns:
                emit.set("namespace", ns)

        return [elem]


@expose_action("on_process_start")
class OnProcessStart(EventHandler):
    """<on_process_start> / Python OnProcessStart shim."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        return cls._parse_xml(entity, parser)


@expose_action("on_process_exit")
class OnProcessExit(EventHandler):
    """<on_process_exit> / Python OnProcessExit shim."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        return cls._parse_xml(entity, parser)


@expose_action("on_state_transition")
class OnStateTransition(EventHandler):
    """<on_state_transition> / Python OnStateTransition shim."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        return cls._parse_xml(entity, parser)


@expose_action("on_shutdown")
class OnShutdown(EventHandler):
    """<on_shutdown> / Python OnShutdown shim."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        return cls._parse_xml(entity, parser)


@expose_action("emit_event")
class EmitEvent(Action):
    """<emit_event> — an event emission.

    Ad-hoc XML parse — no official XML counterpart.
    """

    def __init__(self, *, event: str = "", target_node=None, namespace=None, **kwargs):
        super().__init__(**kwargs)
        self.event = event
        self.target_node = target_node
        self.namespace = namespace

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        event = entity.get_attr("event", optional=True) or ""
        target_node = entity.get_attr("target_node", optional=True)
        ee_ns = entity.get_attr("namespace", optional=True)
        return cls(event=event, target_node=target_node, namespace=ee_ns)

    def execute(self, context) -> list:
        from roscope.entities.helpers import resolve_value

        event = resolve_value(self.event, context) or ""
        target_node = resolve_value(self.target_node, context)
        namespace = resolve_value(self.namespace, context)
        return [EmitEvent(event=event, target_node=target_node, namespace=namespace)]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.event:
            return []
        elem = ET.Element("emit_event")
        elem.set("event", self.event)
        if self.target_node:
            elem.set("target_node", self.target_node)
        if self.namespace:
            elem.set("namespace", self.namespace)
        return [elem]


# ─── Python-shim-only classes ────────────────────────────────────────────────
# These exist solely so the import patcher can install them for user Python
# launch files.  They don't produce resolved output.


class TrackedEmitEvent(Action):
    """Python shim for ``launch.actions.EmitEvent``."""

    def __init__(self, event=None, **kwargs):
        super().__init__(**kwargs)
        self._event = event


class ChangeState(Action):
    """Python shim for ``lifecycle_msgs.msg.Transition``."""

    def __init__(self, lifecycle_node_matcher=None, transition_id=None, **kwargs):
        super().__init__(**kwargs)


class Shutdown(Action):
    """Python shim for ``launch.actions.Shutdown``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class RegisterEventHandler(Action):
    """Python shim for ``launch.actions.RegisterEventHandler``.

    Event-handler-driven topology changes are not resolved — the resolved output
    will not include nodes or actions that are only launched in response to an
    event.  A warning is emitted so that users are aware of this gap.
    """

    def __init__(self, event_handler=None, **kwargs):
        super().__init__(**kwargs)
        self._event_handler = event_handler

    def execute(self, context) -> list:
        logger.warning(
            "RegisterEventHandler encountered — event-handler-driven topology "
            "is not resolved; nodes or actions triggered by events will not "
            "appear in the resolved output"
        )
        return []
