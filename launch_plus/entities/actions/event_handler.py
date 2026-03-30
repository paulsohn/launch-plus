"""Action handlers for event-related elements.

Covers: <on_process_start>, <on_process_exit>, <on_state_transition>,
<on_shutdown>, <emit_event>.
"""

from __future__ import annotations

from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("on_process_start")
@expose_action("on_process_exit")
@expose_action("on_state_transition")
@expose_action("on_shutdown")
class _EventHandlerAction(_TrackedAction):
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

    def execute(self, context) -> list | None:
        from launch_plus.entities.xml_resolver import resolve_value

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
        _R._track_event_handler(
            context._state,
            {
                "handler_kind": self._handler_kind,
                "target": resolve_value(self._target, context),
                "target_node": resolve_value(self._target_node, context),
                "start_state": resolve_value(self._start_state, context),
                "goal_state": resolve_value(self._goal_state, context),
                "namespace_stack": list(context._state.namespace_stack),
                "explicit_namespace": resolve_value(self._handler_ns, context),
                "actions": eh_actions,
            },
        )
        return None


@expose_action("emit_event")
class _EmitEventAction(_TrackedAction):
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

    def execute(self, context) -> list | None:
        from launch_plus.entities.xml_resolver import resolve_value

        event = resolve_value(self._event, context) or ""
        target_node = resolve_value(self._target_node_attr, context)
        ee_ns = resolve_value(self._namespace, context)
        _R._track_event_handler(
            context._state,
            {
                "handler_kind": "emit_event",
                "target": None,
                "target_node": target_node,
                "start_state": None,
                "goal_state": None,
                "namespace_stack": list(context._state.namespace_stack),
                "explicit_namespace": ee_ns,
                "actions": [
                    {
                        "event": event,
                        "target_node": target_node,
                        "namespace_stack": list(context._state.namespace_stack),
                        "explicit_namespace": ee_ns,
                    }
                ],
            },
        )
        return None


# ─── Python-shim actions ─────────────────────────────────────────────────────

import launch_plus.resolver as _R  # noqa: E402
from launch_plus.entities.actions.base import _TrackedAction  # noqa: E402


def _transition_name(transition_id):
    """Convert a lifecycle Transition constant to a human-readable name."""
    _TRANSITION_MAP = {
        1: "configure",
        2: "cleanup",
        3: "activate",
        4: "deactivate",
        5: "shutdown",
        6: "shutdown",
        7: "shutdown",
    }
    if isinstance(transition_id, int):
        return _TRANSITION_MAP.get(transition_id, f"transition_{transition_id}")
    return str(transition_id) if transition_id else ""


def _action_name(action, state):
    """Extract the node name from a tracked action for event handler targeting."""
    if action is not None and hasattr(action, "_idx") and action._idx >= 0:
        return state.tracked["nodes"][action._idx].get("name", "")
    return ""


def _action_namespace_info(action, state):
    """Extract namespace_stack and explicit_namespace for a tracked node action."""
    if action is None or not hasattr(action, "_idx") or action._idx < 0:
        return [], None
    entry = state.tracked["nodes"][action._idx]
    return entry.get("namespace_stack", []), entry.get("explicit_namespace")


class _TrackedEmitEvent(_TrackedAction):
    """Tracked emit_event — records the event type and optional target node."""

    def __init__(self, event=None, **kwargs):
        self._event = event
        self._target_node = None
        self._namespace_stack = []
        self._explicit_namespace = None
        if event is not None and hasattr(event, "_target_node"):
            self._target_node = event._target_node
        if event is not None and hasattr(event, "_event_name"):
            self._event = event._event_name
        if event is not None and hasattr(event, "_namespace_stack"):
            self._namespace_stack = event._namespace_stack
            self._explicit_namespace = event._explicit_namespace

    def to_dict(self):
        return {
            "event": str(self._event) if self._event else "",
            "target_node": self._target_node,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
        }


class _TrackedChangeState(_TrackedAction):
    """Tracked ChangeState event — records the transition and target node."""

    def __init__(self, lifecycle_node_matcher=None, transition_id=None, **kwargs):
        self._event_name = _transition_name(transition_id)
        self._matcher = lifecycle_node_matcher
        self._target_node = None
        self._namespace_stack = []
        self._explicit_namespace = None

    def _resolve_matcher(self, state):
        """Resolve matcher to target node info using state."""
        matcher = self._matcher
        if matcher is not None:
            if hasattr(matcher, "_resolve"):
                matcher._resolve(state)
                self._target_node = matcher._node_name
                self._namespace_stack = matcher._namespace_stack
                self._explicit_namespace = matcher._explicit_namespace
            elif hasattr(matcher, "_node_name"):
                self._target_node = matcher._node_name
                self._namespace_stack = getattr(matcher, "_namespace_stack", [])
                self._explicit_namespace = getattr(matcher, "_explicit_namespace", None)


class _TrackedShutdown(_TrackedAction):
    """Tracked Shutdown event."""

    _event_name = "shutdown"

    def __init__(self, **kwargs):
        if kwargs:
            _R.get_state().error(
                f"Shutdown event arguments are not yet supported: "
                f"{', '.join(f'{k}={v!r}' for k, v in kwargs.items())}"
            )

    def to_dict(self):
        return {
            "event": "shutdown",
            "target_node": None,
            "namespace_stack": [],
            "explicit_namespace": None,
        }


class _TrackedMatchesAction(_TrackedAction):
    """Wraps ``matches_action(node)`` — carries the raw action for deferred name resolution."""

    def __init__(self, action):
        self._action = action
        # Cached after first resolve
        self._node_name = None
        self._namespace_stack = []
        self._explicit_namespace = None

    def _resolve(self, state):
        if self._node_name is None:
            self._node_name = _action_name(self._action, state)
            self._namespace_stack, self._explicit_namespace = _action_namespace_info(
                self._action, state
            )

    def __call__(self, *args, **kwargs):
        return True


class _TrackedOnProcessStart(_TrackedAction):
    """Tracked OnProcessStart event handler."""

    def __init__(self, target_action=None, on_start=None, **kwargs):
        self._target_action = target_action
        self._actions = on_start or []

    def to_event_handler(self, state=None):
        if state is None:
            state = _R.get_state()
        target_name = _action_name(self._target_action, state)
        ns_stack, explicit_ns = _action_namespace_info(self._target_action, state)
        return {
            "handler_kind": "on_process_start",
            "target": target_name,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": ns_stack,
            "explicit_namespace": explicit_ns,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnProcessExit(_TrackedAction):
    """Tracked OnProcessExit event handler."""

    def __init__(self, target_action=None, on_exit=None, **kwargs):
        self._target_action = target_action
        self._actions = on_exit or []

    def to_event_handler(self, state=None):
        if state is None:
            state = _R.get_state()
        target_name = _action_name(self._target_action, state)
        ns_stack, explicit_ns = _action_namespace_info(self._target_action, state)
        return {
            "handler_kind": "on_process_exit",
            "target": target_name,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": ns_stack,
            "explicit_namespace": explicit_ns,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnStateTransition(_TrackedAction):
    """Tracked OnStateTransition event handler."""

    def __init__(
        self, target_lifecycle_node=None, start_state=None, goal_state=None, entities=None, **kwargs
    ):
        self._target_lifecycle_node = target_lifecycle_node
        self._start_state = str(start_state) if start_state else None
        self._goal_state = str(goal_state) if goal_state else None
        self._actions = entities or []

    def to_event_handler(self, state=None):
        if state is None:
            state = _R.get_state()
        target_node = _action_name(self._target_lifecycle_node, state)
        ns_stack, explicit_ns = _action_namespace_info(self._target_lifecycle_node, state)
        return {
            "handler_kind": "on_state_transition",
            "target": None,
            "target_node": target_node or None,
            "start_state": self._start_state,
            "goal_state": self._goal_state,
            "namespace_stack": ns_stack,
            "explicit_namespace": explicit_ns,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnShutdown(_TrackedAction):
    """Tracked OnShutdown event handler."""

    def __init__(self, on_shutdown=None, **kwargs):
        self._actions = on_shutdown or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_shutdown",
            "target": None,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": [],
            "explicit_namespace": None,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedRegisterEventHandler(_TrackedAction):
    """Tracked RegisterEventHandler — records the event handler to _state.tracked."""

    def __init__(self, event_handler=None, **kwargs):
        self._event_handler = event_handler

    def execute(self, context) -> list | None:
        eh = self._event_handler
        if eh is not None and hasattr(eh, "to_event_handler"):
            _R._track_event_handler(context._state, eh.to_event_handler())
        return None
