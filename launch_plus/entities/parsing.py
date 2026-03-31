"""Action parser facade for XML/YAML action handlers.

Provides :class:`_ActionParser`, a stateless parsing helper that action
``parse()`` classmethods use to build unresolved action instances.
"""

from __future__ import annotations

from launch_plus.entities.helpers import _evaluate_condition
from launch_plus.parsers.entity import Entity


class _ActionParser:
    """Stateless parsing helper for action ``parse()`` classmethods.

    Provides substitution token parsing and condition evaluation.
    Does NOT resolve substitutions or access mutable state — ``parse()``
    methods use this to build unresolved action instances.
    """

    __slots__ = ("ctx", "include_stack")

    def __init__(self, ctx, include_stack: list[str]) -> None:
        self.ctx = ctx
        self.include_stack = include_stack

    def parse_substitution(self, text: str) -> list:
        """Parse ``$(...)`` substitutions in *text* into token objects.

        Returns a list of :class:`Substitution` objects that can be resolved
        later via ``resolve_substitutions_from_tokens()``.
        """
        from launch_plus.parsers.parse_substitution import parse_substitution as _lark_parse

        return _lark_parse(text)

    def evaluate_condition(self, entity: Entity) -> bool:
        """Evaluate if=/unless= on *entity*.  Returns True → element should execute."""
        if_val = entity.get_attr("if", optional=True)
        unless_val = entity.get_attr("unless", optional=True)
        cond: dict[str, str] | None = None
        if if_val is not None:
            cond = {"kind": "If", "expr": if_val}
        elif unless_val is not None:
            cond = {"kind": "Unless", "expr": unless_val}
        return _evaluate_condition(cond, self.ctx)

    # ── Unresolved extraction from Entity children ───────────────────

    def parse_params(self, entity: Entity) -> list:
        """Extract <param> children as unresolved token structures.

        Returns a list of dicts, each either:
        - ``{"name": tokens, "value": tokens}`` for inline params
        - ``{"from": tokens}`` for param file references
        """
        items = entity.get_attr("param", data_type=list, optional=True)
        if not items:
            return []
        result = []
        for p in items:
            name = p.get_attr("name", optional=True)
            value = p.get_attr("value", optional=True)
            from_file = p.get_attr("from", optional=True)
            if from_file:
                result.append({"from": self.parse_substitution(from_file)})
            elif name:
                result.append(
                    {
                        "name": self.parse_substitution(name),
                        "value": self.parse_substitution(value or ""),
                    }
                )
        return result

    def parse_remaps(self, entity: Entity) -> list:
        """Extract <remap> children as unresolved token pairs."""
        items = entity.get_attr("remap", data_type=list, optional=True)
        if not items:
            return []
        return [
            (
                self.parse_substitution(r.get_attr("from", optional=True) or ""),
                self.parse_substitution(r.get_attr("to", optional=True) or ""),
            )
            for r in items
        ]

    def parse_envs(self, entity: Entity) -> list:
        """Extract <env> children as unresolved token pairs."""
        items = entity.get_attr("env", data_type=list, optional=True)
        if not items:
            return []
        return [
            (
                self.parse_substitution(e.get_attr("name", optional=True) or ""),
                self.parse_substitution(e.get_attr("value", optional=True) or ""),
            )
            for e in items
        ]

    def parse_composable_plugins(self, entity: Entity) -> list:
        """Extract <composable_node> children as ComposableNode instances.

        Matching official: ``ComposableNode.parse(parser, entity)`` creates
        ``ComposableNode`` instances, not raw dicts.
        """
        from launch_plus.entities.actions.node import ComposableNode

        items = entity.get_attr("composable_node", data_type=list, optional=True)
        if not items:
            return []
        plugins = []
        for cn in items:
            if not self.evaluate_condition(cn):
                continue
            name_raw = cn.get_attr("name", optional=True)
            plugins.append(
                ComposableNode(
                    package=self.parse_substitution(cn.get_attr("pkg", optional=True) or ""),
                    plugin=self.parse_substitution(cn.get_attr("plugin", optional=True) or ""),
                    name=self.parse_substitution(name_raw) if name_raw else None,
                    parameters=self.parse_params(cn),
                    remappings=self.parse_remaps(cn),
                )
            )
        return plugins
