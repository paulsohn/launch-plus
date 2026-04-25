# Copyright 2018 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Originally from:
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/include_launch_description.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the IncludeLaunchDescription action."""

from __future__ import annotations

import logging
import os

from roscope.entities.action import Action
from roscope.entities.actions.group_action import GroupAction
from roscope.entities.actions.marker import ArgComment, SourceMarker
from roscope.entities.actions.opaque_function import visit_actions
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _current_file, resolve_value
from roscope.entities.parsing import Parser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("include")
class IncludeLaunchDescription(Action):
    """Action that includes a launch description source and yields its entities when visited."""

    def __init__(self, launch_description_source, *, launch_arguments=None, **kwargs):
        super().__init__(**kwargs)
        self.launch_description_source = launch_description_source
        self.launch_arguments = launch_arguments
        self.include_stack: list = kwargs.get("include_stack", [])
        # Lazily resolved path (for Python shim sources)
        self.path: str | None = None

        loc = getattr(launch_description_source, "_location", None)
        if isinstance(loc, str):
            self.path = loc

    @classmethod
    def parse(cls, entity: Entity, parser: Parser):
        _, kwargs = super().parse(entity, parser)
        file_path = parser.parse_substitution(entity.get_attr("file"))
        kwargs["launch_description_source"] = file_path
        kwargs["include_stack"] = list(parser.include_stack)
        args = []
        args_arg = entity.get_attr("arg", data_type=list, optional=True)
        if args_arg is not None:
            args.extend(args_arg)
        args_let = entity.get_attr("let", data_type=list, optional=True)
        if args_let is not None:
            args.extend(args_let)
        if args:
            kwargs["launch_arguments"] = [
                (
                    parser.parse_substitution(e.get_attr("name")),
                    parser.parse_substitution(e.get_attr("value")),
                )
                for e in args
            ]
        return cls, kwargs

    def execute(self, context) -> list:
        """Resolve the included file and return resolved actions wrapped with markers."""
        state = context._state

        # Step 1: Resolve file path
        file_path = self._resolve_file_path(context)
        if not file_path:
            logger.error(
                "%s: include: 'file' attribute resolved to empty string", _current_file(context)
            )
            return []

        # Step 2: Validate
        if len(self.include_stack) > 20:
            logger.warning(
                "%s: max include depth exceeded for %s", _current_file(context), file_path
            )
            return []

        # Step 3: Track include
        dep_idx = state.track_include(
            file_path,
            ros_namespace=context._launch_configurations.get("ros_namespace"),
        )

        # Step 4: Resolve include arguments
        child_args = self._resolve_args(context, dep_idx)

        # Step 5: Check file exists
        if not os.path.isfile(file_path):
            logger.error(
                "%s: included launch file not found: %s", _current_file(context), file_path
            )
            return []

        # Step 6: Resolve children
        children = resolve_included_file(
            context, self.include_stack, file_path, file_path, child_args
        )

        # Collect args declared in the immediate child file (not transitive includes)
        # that were not explicitly passed — use the stored declared default, not the
        # current _launch_configurations value (which may have been overwritten by <let>
        # or inherited from a different context).
        child_declared_defaults: dict[str, tuple[str, bool]] = {}
        for name, (declared_default, effective) in state.declared_arg_names_by_file.get(
            file_path, {}
        ).items():
            if name not in child_args:
                child_declared_defaults[name] = (effective, effective == declared_default)

        # Step 8: Wrap with markers
        return _wrap_with_markers(children, file_path, child_args, child_declared_defaults, state)

    def _resolve_file_path(self, context) -> str | None:
        """Resolve the file path.."""

        src = self.launch_description_source
        if hasattr(src, "_resolve_location"):
            self.path = src._resolve_location(context)
        else:
            try:
                self.path = resolve_value(src, context)
            except Exception as e:
                logger.warning(
                    "%s: failed to resolve IncludeLaunchDescription source: %s",
                    _current_file(context),
                    e,
                )

        return self.path

    def _resolve_args(self, context, dep_idx: int) -> dict[str, str]:
        """Resolve include arguments."""

        state = context._state
        child_args: dict[str, str] = {}

        # Temporarily set each arg in context so later args can reference earlier ones,
        # then restore — matching the original XML path behavior.
        saved_lc = dict(context._launch_configurations)
        for k, v in self.launch_arguments or []:
            k_str = resolve_value(k, context) or ""
            child_args[k_str] = resolve_value(v, context) or ""
            context._launch_configurations[k_str] = child_args[k_str]
        context._launch_configurations.clear()
        context._launch_configurations.update(saved_lc)

        # Record args for dependency tracking
        if dep_idx >= 0 and child_args:
            state.include_deps[dep_idx]["include_args"] = child_args

        return child_args


def _wrap_with_markers(
    children, file_path, args, child_declared_defaults: dict[str, tuple[str, bool]], state
) -> list:
    """Wrap resolved children in a GroupAction with SourceMarker as first child.

    The GroupAction holds [SourceMarker, ArgComment..., ...children].

    ``args`` — explicitly passed include arguments (name → resolved value).
    ``child_declared_defaults`` — args declared in the child but not explicitly passed
    (name → (effective_value, is_default)).  ``is_default`` is True when the effective
    value matches the declared default, False when it was inherited from a parent context.
    """
    has_content = any(not isinstance(c, SourceMarker) for c in children)
    if has_content or state.show_empty_includes:
        group_children: list = [SourceMarker(file_path, args)]

        # Conditionally add arg markers (--show-args)
        if state.show_args:
            merged: dict[str, tuple[str, bool]] = {}
            for name, value in args.items():
                merged[name] = (value, False)
            for name, (value, is_def) in child_declared_defaults.items():
                merged.setdefault(name, (value, is_def))
            group_children.extend(
                ArgComment(name=k, value=v, is_default=is_def)
                for k, (v, is_def) in sorted(merged.items())
            )

        group = GroupAction(resolved_children=group_children + list(children))
        return [group]
    return list(children)


def _inline_resolve_python_launch(state, launch_file, parent_context, child_args) -> list:
    """Load a Python launch file and walk its actions in the parent context."""
    import importlib.util

    real_path = launch_file
    if not os.path.isfile(real_path):
        return []

    try:
        spec = importlib.util.spec_from_file_location(
            f"_inline_launch_{len(state.include_chain)}", real_path
        )
        if spec is None or spec.loader is None:
            logger.error("cannot load included launch file: %s", real_path)
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.error("failed to load included launch file %s: %s", real_path, e)
        return []

    if not hasattr(mod, "generate_launch_description"):
        return []

    saved_declared_arg_names = set(state.declared_arg_names)
    try:
        try:
            ld = mod.generate_launch_description()
        except Exception as e:
            logger.error("generate_launch_description() failed in %s: %s", launch_file, e)
            return []

        entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

        for k, v in child_args.items():
            parent_context._launch_configurations[k] = v

        return visit_actions(entities, parent_context)
    finally:
        state.declared_arg_names.clear()
        state.declared_arg_names.update(saved_declared_arg_names)


def resolve_included_file(
    ctx,
    include_stack: list[str],
    real_path: str,
    file_path: str,
    child_ctx_args: dict[str, str],
) -> list:
    """Parse an included launch file and return resolved actions."""
    from roscope.resolver import resolve_xml_elements

    state = ctx._state
    state.include_chain.append(file_path)
    new_stack = include_stack + [file_path]
    for k, v in child_ctx_args.items():
        ctx._launch_configurations[k] = v
    saved_launch_file_dir = ctx.launch_file_dir
    ctx.launch_file_dir = os.path.dirname(real_path)
    results: list = []
    try:
        if real_path.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
            from roscope.parsers.xml_parser import parse_xml_launch
            from roscope.parsers.yaml_parser import parse_yaml_launch

            with open(real_path) as f:
                content = f.read()
            child_entities: list
            if real_path.endswith((".yaml", ".yml")):
                child_entities = list(parse_yaml_launch(content, real_path))
            else:
                child_entities = list(parse_xml_launch(content, real_path))
            results = resolve_xml_elements(child_entities, ctx, include_stack=new_stack)
        elif real_path.endswith((".launch.py", ".py")):
            results = _inline_resolve_python_launch(state, file_path, ctx, child_ctx_args)
    finally:
        ctx.launch_file_dir = saved_launch_file_dir
        state.include_chain.pop()
    return results
