"""Action handler for <include> element."""

from __future__ import annotations

import os

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.state import (
    _StubLaunchContext,
    _warn,
)
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _parse_portable_path,
    _resolve_pkg_share,
)


@expose_action("include")
class _TrackedIncludeLaunchDescription(_TrackedAction):
    """Tracks IncludeLaunchDescription for both XML and Python shim paths."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser) -> None:
        if not parser.evaluate_condition(entity):
            return
        raw_file = entity.get_attr("file", optional=True) or ""
        file_path = parser.resolve(raw_file)

        # Check for unportable absolute paths in preview mode
        if (
            parser.state.preview_mode
            and os.path.isabs(file_path)
            and "$(find-pkg-share" not in raw_file
            and "$(dirname)" not in raw_file
        ):
            if parser.state.allow_unportable_paths:
                parser.state.warn(f"unportable absolute path in include: {file_path}")
            else:
                parser.state.error(f"unportable absolute path in include: {file_path}")

        if file_path in parser.include_stack:
            parser.state.error(f"circular include detected: {file_path}")
            return
        if len(parser.include_stack) > 20:
            parser.state.warn(f"max include depth exceeded for {file_path}")
            return
        dep_idx = parser.track_include(file_path)
        child_ctx_args = parser.resolve_include_args(entity)
        if dep_idx >= 0 and child_ctx_args:
            parser.state.tracked["include_deps"][dep_idx]["include_args"] = child_ctx_args
        if child_ctx_args:
            parser.state.tracked["include_args"][file_path] = child_ctx_args
        real_path = file_path
        parsed_path = _parse_portable_path(file_path)
        if parsed_path:
            pkg, rest = parsed_path
            try:
                pkg_share = _resolve_pkg_share(pkg)
                real_path = os.path.join(pkg_share, rest)
            except Exception:
                return
        if os.path.isfile(real_path):
            parser.parse_and_resolve_included_file(real_path, file_path, child_ctx_args)

    def __init__(self, launch_description_source, launch_arguments=None, **kwargs):
        self._source = launch_description_source
        self._raw_launch_arguments = launch_arguments
        path = None
        if hasattr(launch_description_source, "_location"):
            path = launch_description_source._location
        elif hasattr(launch_description_source, "location"):
            try:
                path = launch_description_source.location
            except Exception:
                pass
        self._path = path
        self._dep_idx = -1
        if path:
            self._dep_idx = _R._track_include(path)

        if launch_arguments and path:
            _resolve_include_args(path, launch_arguments, _StubLaunchContext(), self._dep_idx)

    def execute(self, context) -> list | None:
        if self._path is None and context is not None:
            src = self._source
            path = None
            if hasattr(src, "perform"):
                try:
                    path = src.perform(context)
                except Exception as e:
                    _warn(f"failed to resolve IncludeLaunchDescription source: {e}")
            if path:
                self._path = path
                dep_idx = _R._track_include(path)
                _resolve_include_args(path, self._raw_launch_arguments, context, dep_idx)

        if self._path and self._path.endswith(".py") and context is not None:
            child_args = {}
            if self._raw_launch_arguments:
                for k, v in self._raw_launch_arguments:
                    k_str = str(k)
                    resolved = _R._resolve_substitution(v, context)
                    v_str = resolved if resolved is not None else str(v)
                    child_args[k_str] = v_str
            inc_dep = _R._extract_pkg_and_share_path(self._path)
            if inc_dep:
                _R._state.include_chain.append(list(inc_dep))
            else:
                _R._state.include_chain.append(["", self._path])
            try:
                _R._inline_resolve_python_launch(self._path, context, child_args)
            finally:
                _R._state.include_chain.pop()

        return None


def _resolve_include_args(path, launch_arguments, context, dep_idx=-1):
    """Capture launch_arguments for an include site."""
    if not launch_arguments or not path:
        return
    path = str(path)
    if dep_idx >= 0 and _R._state.tracked["include_deps"][dep_idx].get("include_args"):
        return
    if dep_idx < 0 and path in _R._state.tracked["include_args"]:
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
                _R._state.tracked["include_deps"][dep_idx]["include_args"] = captured
            else:
                _R._state.tracked["include_args"][path] = captured
    except Exception as e:
        _warn(f"failed to resolve include args for '{path}': {e}")
