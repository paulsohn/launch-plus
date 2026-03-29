"""Action handler for <include> element."""

from __future__ import annotations

import os

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _error,
    _PackageNotFetchedError,
    _parse_portable_path,
    _resolve_pkg_share,
    _state,
    _track_include,
    _warn,
)


@expose_action("include")
def _action_include(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    raw_file = entity.get_attr("file", optional=True) or ""
    file_path = parser.resolve(raw_file)

    # Check for unportable absolute paths in preview mode
    if (
        _state.preview_mode
        and os.path.isabs(file_path)
        and "$(find-pkg-share" not in raw_file
        and "$(dirname)" not in raw_file
    ):
        if _state.allow_unportable_paths:
            _warn(f"unportable absolute path in include: {file_path}")
        else:
            _error(f"unportable absolute path in include: {file_path}")

    if file_path in parser.include_stack:
        _error(f"circular include detected: {file_path}")
        return
    if len(parser.include_stack) > 20:
        _warn(f"max include depth exceeded for {file_path}")
        return
    dep_idx = _track_include(file_path)
    child_ctx_args = parser.resolve_include_args(entity)
    if dep_idx >= 0 and child_ctx_args:
        _state.tracked["include_deps"][dep_idx]["include_args"] = child_ctx_args
    if child_ctx_args:
        _state.tracked["include_args"][file_path] = child_ctx_args
    real_path = file_path
    parsed_path = _parse_portable_path(file_path)
    if parsed_path:
        pkg, rest = parsed_path
        try:
            pkg_share = _resolve_pkg_share(pkg)
            real_path = os.path.join(pkg_share, rest)
        except _PackageNotFetchedError:
            raise
        except Exception:
            return
    if os.path.isfile(real_path):
        parser.parse_and_resolve_included_file(real_path, file_path, child_ctx_args)
