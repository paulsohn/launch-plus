"""Action handler for <include> element."""

from __future__ import annotations

import logging
import os

from launch_plus.entities.action import Action
from launch_plus.entities.actions.group import GroupAction
from launch_plus.entities.actions.marker import ArgComment, EndSourceMarker, SourceMarker
from launch_plus.entities.expose import expose_action
from launch_plus.entities.helpers import (
    _extract_pkg_and_share_path,
    _parse_portable_path,
)
from launch_plus.entities.parsing import _ActionParser
from launch_plus.entities.substitution import Substitution
from launch_plus.parsers.entity import Entity

logger = logging.getLogger("launch_plus")


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
        from launch_plus.resolver import resolve_included_file

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

        # Step 3: Check unportable paths (XML path only)
        if (
            self.raw_file
            and state.preview_mode
            and os.path.isabs(file_path)
            and "$(find-pkg-share" not in self.raw_file
            and "$(dirname)" not in self.raw_file
        ):
            if state.allow_unportable_paths:
                logger.warning("unportable absolute path in include: %s", file_path)
            else:
                logger.error("unportable absolute path in include: %s", file_path)

        # Step 4: Track include
        dep_idx = state.track_include(
            file_path,
            ros_namespace=context._launch_configurations.get("ros_namespace"),
        )

        # Step 5: Resolve include arguments
        child_args = self._resolve_args(context, file_path, dep_idx)

        # Step 6: Resolve real filesystem path
        real_path = file_path
        parsed_path = _parse_portable_path(file_path)
        if parsed_path:
            pkg, rest = parsed_path
            try:
                pkg_share = state.resolve_pkg_share(pkg)
                real_path = os.path.join(pkg_share, rest)
            except Exception:
                return []
        if not os.path.isfile(real_path):
            return []

        # Step 7: Resolve children
        children = resolve_included_file(
            context, self.include_stack, real_path, file_path, child_args
        )

        # Step 8: Wrap with markers
        inc_dep = _extract_pkg_and_share_path(file_path)
        pkg = inc_dep[0] if inc_dep else ""
        share = inc_dep[1] if inc_dep else file_path
        return _wrap_with_markers(children, pkg, share, child_args, state)

    def _resolve_file_path(self, context) -> str | None:
        """Resolve the file path from tokens (XML) or source object (Python shim)."""
        from launch_plus.entities.helpers import resolve_value

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
        from launch_plus.entities.helpers import resolve_value

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
    """Wrap resolved children in [SourceMarker, GroupAction, EndSourceMarker].

    The GroupAction holds ArgComment markers (explicit + declared defaults)
    followed by the resolved children.
    """
    has_content = any(not isinstance(c, (SourceMarker, EndSourceMarker)) for c in children)
    if has_content or state.show_empty_includes:
        # Build arg markers: explicit args + declared defaults
        source_key = f"{pkg}://{share}" if pkg else share
        declared = state.tracked.get("declared_args_by_file", {}).get(source_key, [])
        merged: dict[str, tuple[str, bool]] = {}
        for name, value in sorted(args.items()):
            merged[name] = (value, False)
        for entry in declared:
            if entry["name"] not in merged:
                merged[entry["name"]] = (entry["default"], True)
        arg_markers = [
            ArgComment(name=k, value=v, is_default=is_def)
            for k, (v, is_def) in sorted(merged.items())
        ]

        group = GroupAction(resolved_children=arg_markers + list(children))
        return [SourceMarker(pkg, share, args), group, EndSourceMarker(pkg, share)]
    return list(children)
