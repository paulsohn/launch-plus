"""Action handler for <include> element."""

from __future__ import annotations

import logging
import os

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.helpers import (
    _extract_pkg_and_share_path,
    _track_include,
)
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _parse_portable_path,
    _resolve_pkg_share,
)

logger = logging.getLogger("launch_plus")


@expose_action("include")
class _TrackedIncludeLaunchDescription(_TrackedAction):
    """Tracks IncludeLaunchDescription for both XML and Python shim paths."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        raw_file = entity.get_attr("file", optional=True) or ""
        file_tokens = parser.parse_substitution(raw_file)
        # Extract raw <arg> children (name → value_tokens pairs)
        arg_items = entity.get_attr("arg", data_type=list, optional=True) or []
        xml_args = []
        for a in arg_items:
            arg_name = a.get_attr("name", optional=True) or ""
            arg_value = a.get_attr("value", optional=True)
            if arg_value is not None:
                xml_args.append((arg_name, parser.parse_substitution(arg_value)))
        return cls(
            launch_description_source=None,
            _xml_file_tokens=file_tokens,
            _xml_raw_file=raw_file,
            _xml_args=xml_args,
            _xml_include_stack=list(parser.include_stack),
            _xml_ctx=parser.ctx,
        )

    def __init__(self, launch_description_source, launch_arguments=None, **kwargs):
        self._source = launch_description_source
        self._raw_launch_arguments = launch_arguments
        # XML path data
        self._xml_file_tokens = kwargs.get("_xml_file_tokens")
        self._xml_raw_file = kwargs.get("_xml_raw_file")
        self._xml_args = kwargs.get("_xml_args")
        self._xml_include_stack = kwargs.get("_xml_include_stack")
        self._xml_ctx = kwargs.get("_xml_ctx")
        # Python shim path — path resolved lazily in _execute_shim()
        # Only set if it's a plain string (no substitutions to resolve)
        self._path = None
        if launch_description_source is not None:
            loc = getattr(launch_description_source, "_location", None)
            if isinstance(loc, str):
                self._path = loc
        self._dep_idx = -1

    def execute(self, context) -> list | None:
        if self._xml_file_tokens is not None:
            return self._execute_xml(context)
        return self._execute_shim(context)

    def _execute_xml(self, ctx) -> list | None:
        """Execute for XML path: resolve file, include args, process included file."""
        from launch_plus.entities.helpers import resolve_value

        file_path = resolve_value(self._xml_file_tokens, ctx) or ""

        # Check for unportable absolute paths
        if (
            ctx._state.preview_mode
            and os.path.isabs(file_path)
            and "$(find-pkg-share" not in (self._xml_raw_file or "")
            and "$(dirname)" not in (self._xml_raw_file or "")
        ):
            if ctx._state.allow_unportable_paths:
                logger.warning("unportable absolute path in include: %s", file_path)
            else:
                logger.error("unportable absolute path in include: %s", file_path)

        include_stack = self._xml_include_stack or []
        if file_path in include_stack:
            logger.error("circular include detected: %s", file_path)
            return None
        if len(include_stack) > 20:
            logger.warning("max include depth exceeded for %s", file_path)
            return None

        dep_idx = _track_include(ctx._state, file_path)
        # Resolve include args sequentially — each arg can reference previous ones.
        # Temporarily set resolved args in parent ctx so $(var x) works for
        # subsequent args; restore parent state afterward.
        saved_lc = dict(ctx._launch_configurations)
        child_ctx_args: dict[str, str] = {}
        for arg_name, value_tokens in self._xml_args or []:
            child_ctx_args[arg_name] = resolve_value(value_tokens, ctx) or ""
            ctx._launch_configurations[arg_name] = child_ctx_args[arg_name]
        # Restore parent context — child args don't leak into parent scope.
        # Use clear+update (not assignment) to preserve dict identity, since
        # outer scopes (e.g., <group>) may hold references to the same dict.
        ctx._launch_configurations.clear()
        ctx._launch_configurations.update(saved_lc)
        if dep_idx >= 0 and child_ctx_args:
            ctx._state.tracked["include_deps"][dep_idx]["include_args"] = child_ctx_args
        if child_ctx_args:
            ctx._state.tracked["include_args"][file_path] = child_ctx_args

        real_path = file_path
        parsed_path = _parse_portable_path(file_path)
        if parsed_path:
            pkg, rest = parsed_path
            try:
                pkg_share = _resolve_pkg_share(ctx._state, pkg)
                real_path = os.path.join(pkg_share, rest)
            except Exception:
                return None
        if os.path.isfile(real_path):
            from launch_plus.resolver import resolve_included_file

            resolve_included_file(ctx, include_stack, real_path, file_path, child_ctx_args)
        return None

    def _execute_shim(self, context) -> list | None:
        # Resolve path lazily using the real context (matches official ROS 2
        # LaunchDescriptionSource.get_launch_description(context) pattern)
        if self._path is None and self._source is not None and context is not None:
            src = self._source
            if hasattr(src, "_resolve_location"):
                # Our deferred source — resolve with real context
                self._path = src._resolve_location(context)
            elif hasattr(src, "perform"):
                try:
                    self._path = src.perform(context)
                except Exception as e:
                    logger.warning("failed to resolve IncludeLaunchDescription source: %s", e)

        if self._path and self._dep_idx < 0:
            self._dep_idx = _track_include(context._state, self._path)
            _resolve_include_args(self._path, self._raw_launch_arguments, context, self._dep_idx)

        if self._path and self._path.endswith(".py") and context is not None:
            child_args = {}
            if self._raw_launch_arguments:
                for k, v in self._raw_launch_arguments:
                    k_str = str(k)
                    resolved = context.perform_substitution(v)
                    v_str = resolved if resolved is not None else str(v)
                    child_args[k_str] = v_str
            inc_dep = _extract_pkg_and_share_path(self._path)
            if inc_dep:
                context._state.include_chain.append(list(inc_dep))
            else:
                context._state.include_chain.append(["", self._path])
            try:
                _R._inline_resolve_python_launch(context._state, self._path, context, child_args)
            finally:
                context._state.include_chain.pop()

        return None


def _resolve_include_args(path, launch_arguments, context, dep_idx=-1):
    """Capture launch_arguments for an include site."""
    if not launch_arguments or not path:
        return
    state = context._state
    path = str(path)
    if dep_idx >= 0 and state.tracked["include_deps"][dep_idx].get("include_args"):
        return
    if dep_idx < 0 and path in state.tracked["include_args"]:
        return
    try:
        captured = {}
        for k, v in launch_arguments:
            k_str = str(k)
            if isinstance(v, list):
                parts = []
                for sub in v:
                    if hasattr(sub, "perform"):
                        try:
                            result = sub.perform(context)
                            parts.append(str(result) if result is not None else str(sub))
                        except Exception:
                            parts.append(str(sub))
                    else:
                        parts.append(str(sub))
                v_str = "".join(parts)
            elif hasattr(v, "perform"):
                try:
                    result = v.perform(context)
                    v_str = str(result) if result is not None else str(v)
                except Exception:
                    v_str = str(v)
            else:
                v_str = str(v)
            captured[k_str] = v_str
        if captured:
            if dep_idx >= 0:
                state.tracked["include_deps"][dep_idx]["include_args"] = captured
            else:
                state.tracked["include_args"][path] = captured
    except Exception as e:
        logger.warning("failed to resolve include args for '%s': %s", path, e)
