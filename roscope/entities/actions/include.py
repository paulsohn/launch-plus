"""Action handler for <include> element."""

from __future__ import annotations

import logging
import os

from roscope.entities.action import Action
from roscope.entities.actions.group import GroupAction
from roscope.entities.actions.marker import ArgComment, SourceMarker
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _extract_pkg_and_share_path
from roscope.entities.parsing import _ActionParser
from roscope.entities.substitution import Substitution
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("include")
class IncludeLaunchDescription(Action):
    """Include another launch file — XML, YAML, or Python.

    Handles both XML parse path (``file=`` attribute with substitution tokens)
    and Python shim path (``launch_description_source`` object).
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        raw_file = entity.get_attr("file", optional=True) or ""
        file_tokens = parser.parse_substitution(raw_file)
        arg_items = entity.get_attr("arg", data_type=list, optional=True) or []
        args = []
        for a in arg_items:
            arg_name = a.get_attr("name", optional=True) or ""
            arg_value = a.get_attr("value", optional=True)
            if arg_value is not None:
                args.append((arg_name, parser.parse_substitution(arg_value)))
        return cls(
            launch_description_source=None,
            file_tokens=file_tokens,
            raw_file=raw_file,
            launch_arguments=args,
            include_stack=list(parser.include_stack),
        )

    def __init__(self, launch_description_source=None, launch_arguments=None, **kwargs):
        self.source = launch_description_source
        self.launch_arguments = launch_arguments
        self.file_tokens = kwargs.get("file_tokens")
        self.raw_file: str = kwargs.get("raw_file", "")
        self.include_stack: list = kwargs.get("include_stack", [])
        # Lazily resolved path (for Python shim sources)
        self.path: str | None = None
        if launch_description_source is not None:
            loc = getattr(launch_description_source, "_location", None)
            if isinstance(loc, str):
                self.path = loc

    def execute(self, context) -> list:
        """Resolve the included file and return resolved actions wrapped with markers."""
        state = context._state

        # Step 1: Resolve file path
        file_path = self._resolve_file_path(context)
        if not file_path:
            return []

        # Step 2: Validate
        if file_path in self.include_stack:
            logger.error("circular include detected: %s", file_path)
            return []
        if len(self.include_stack) > 20:
            logger.warning("max include depth exceeded for %s", file_path)
            return []

        # Step 3: Track include
        dep_idx = state.track_include(
            file_path,
            ros_namespace=context._launch_configurations.get("ros_namespace"),
        )

        # Step 4: Resolve include arguments
        child_args = self._resolve_args(context, file_path, dep_idx)

        # Step 5: Check file exists
        if not os.path.isfile(file_path):
            logger.error("included launch file not found: %s", file_path)
            return []

        # Step 6: Resolve children
        children = resolve_included_file(
            context, self.include_stack, file_path, file_path, child_args
        )

        # Step 8: Wrap with markers
        inc_dep = _extract_pkg_and_share_path(file_path)
        pkg = inc_dep[0] if inc_dep else ""
        share = inc_dep[1] if inc_dep else file_path
        return _wrap_with_markers(children, pkg, share, child_args, state)

    def _resolve_file_path(self, context) -> str | None:
        """Resolve the file path from tokens (XML) or source object (Python shim)."""
        from roscope.entities.helpers import resolve_value

        # XML path: resolve substitution tokens
        if self.file_tokens is not None:
            return resolve_value(self.file_tokens, context) or ""

        # Python shim path: resolve location lazily
        if self.path is None and self.source is not None and context is not None:
            src = self.source
            if hasattr(src, "_resolve_location"):
                self.path = src._resolve_location(context)
            elif isinstance(src, Substitution):
                try:
                    self.path = src.perform(context)
                except Exception as e:
                    logger.warning("failed to resolve IncludeLaunchDescription source: %s", e)

        return self.path

    def _resolve_args(self, context, file_path: str, dep_idx: int) -> dict[str, str]:
        """Resolve include arguments from tokens (XML) or launch_arguments (Python shim)."""
        from roscope.entities.helpers import resolve_value

        state = context._state
        child_args: dict[str, str] = {}

        if self.file_tokens is not None:
            # XML path: resolve tokens sequentially
            saved_lc = dict(context._launch_configurations)
            for arg_name, value_tokens in self.launch_arguments or []:
                child_args[arg_name] = resolve_value(value_tokens, context) or ""
                context._launch_configurations[arg_name] = child_args[arg_name]
            context._launch_configurations.clear()
            context._launch_configurations.update(saved_lc)
        else:
            # Python shim path: resolve substitutions
            for k, v in self.launch_arguments or []:
                k_str = str(k)
                resolved = context.perform_substitution(v)
                child_args[k_str] = resolved if resolved is not None else str(v)

        # Record args for --show-args
        if dep_idx >= 0 and child_args:
            state.tracked["include_deps"][dep_idx]["include_args"] = child_args
        if child_args:
            state.tracked["include_args"][file_path] = child_args

        return child_args


def _wrap_with_markers(children, pkg, share, args, state) -> list:
    """Wrap resolved children in a GroupAction with SourceMarker as first child.

    The GroupAction holds [SourceMarker, ArgComment..., ...children].
    """
    has_content = any(not isinstance(c, SourceMarker) for c in children)
    if has_content or state.show_empty_includes:
        group_children: list = [SourceMarker(pkg, share, args)]

        # Conditionally add arg markers (--show-args)
        if state.show_args:
            source_key = f"{pkg}://{share}" if pkg else share
            declared = state.tracked.get("declared_args_by_file", {}).get(source_key, [])
            merged: dict[str, tuple[str, bool]] = {}
            for name, value in sorted(args.items()):
                merged[name] = (value, False)
            for entry in declared:
                if entry["name"] not in merged:
                    merged[entry["name"]] = (entry["default"], True)
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

    from roscope.entities.actions.arg import DeclareLaunchArgument, _apply_declared_arg
    from roscope.resolver import _execute_actions

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

        for entity in entities:
            if isinstance(entity, DeclareLaunchArgument):
                _apply_declared_arg(entity, parent_context)

        return _execute_actions(entities, parent_context)
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
    inc_dep = _extract_pkg_and_share_path(file_path)
    if inc_dep:
        state.include_chain.append(list(inc_dep))
    else:
        state.include_chain.append(["", file_path])
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
