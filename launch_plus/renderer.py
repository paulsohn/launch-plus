"""Render resolved launch actions as XML."""

from __future__ import annotations

import logging
from pathlib import Path

from launch_plus.types import IncludeArgContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _xml_escape(s: str) -> str:
    """Escape special XML characters in an attribute value."""
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _qualify_topic(topic: str, namespace: str | None) -> str:
    """Qualify a relative topic name with the node's namespace.

    Absolute topics (starting with ``/``) and private topics (starting with
    ``~/``) are returned as-is.  Relative topics are prefixed with the
    node's namespace, matching how the ROS 2 runtime resolves them.
    """
    if not topic or not namespace or topic.startswith("/") or topic.startswith("~/"):
        return topic
    return f"{namespace.rstrip('/')}/{topic}"


def _pad(depth: int) -> str:
    """Indentation string for the given visual depth level."""
    return "  " * (depth + 1)


def _visual_src_depth(open_src_len: int) -> int:
    """Visual source depth — drives indentation."""
    return open_src_len


def _format_source_label(pkg: str, path: Path) -> str:
    """Format a source label for XML comments."""
    if not pkg:
        return str(path)
    return f"{pkg}://{path}"


def _common_prefix_len(
    a: list[tuple[str, Path]],
    b: list[tuple[str, Path]],
) -> int:
    """Length of the common prefix of two lists."""
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _render_show_args(
    key: tuple[str, Path],
    include_args: dict[tuple[str, Path], IncludeArgContext],
    declared_args_by_file: dict[tuple[str, Path], dict[str, str]],
    indent: int,
    out: list[str],
) -> None:
    """Emit ``<!-- arg ... -->`` comments for a given include boundary."""
    ctx = include_args.get(key)
    explicit = dict(ctx.explicit) if ctx else {}
    declared = dict(declared_args_by_file.get(key, {}))

    merged: dict[str, tuple[str, bool]] = {}
    for name, value in explicit.items():
        merged[name] = (value, False)
    for name, value in declared.items():
        if name not in merged:
            merged[name] = (value, True)

    if not merged:
        return

    ind = _pad(indent)
    for name in sorted(merged):
        value, is_default = merged[name]
        if is_default:
            out.append(f'{ind}<!-- arg name="{name}" default="{_xml_escape(value)}" -->\n')
        else:
            out.append(f'{ind}<!-- arg name="{name}" value="{_xml_escape(value)}" -->\n')


# ---------------------------------------------------------------------------
# Source-stack helper
# ---------------------------------------------------------------------------


def _action_source_stack(
    action,
    root_pkg: str,
    root_path: Path,
) -> list[tuple[str, Path]]:
    """Compute the source-group nesting stack for an action."""
    chain = getattr(action, "_include_chain", [])
    if chain:
        chain = [(pkg, Path(sp)) for pkg, sp in chain]
        first_pkg, first_path = chain[0]
        if first_pkg == root_pkg and first_path == root_path:
            return list(chain[1:])
        return list(chain)
    return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_resolved_xml(
    package: str,
    launcher: str,
    actions: list,
    *,
    include_args: dict[tuple[str, Path], IncludeArgContext] | None = None,
    show_args: bool = False,
    initial_args: dict[str, str] | None = None,
    declared_args_by_file: dict[tuple[str, Path], dict[str, str]] | None = None,
) -> str:
    """Render resolved actions as a ``<launch>`` XML document.

    Each action's ``serialize_resolved(indent)`` produces its XML snippet.
    Source-group nesting uses ``action._include_chain``.
    """
    if include_args is None:
        include_args = {}
    if initial_args is None:
        initial_args = {}
    if declared_args_by_file is None:
        declared_args_by_file = {}

    out: list[str] = []
    out.append(f"<!-- resolved by launch-plus from {package}://launch/{launcher} -->\n")
    out.append("<launch>\n")

    if show_args and initial_args:
        for name in sorted(initial_args):
            value = initial_args[name]
            out.append(f'  <!-- arg name="{name}" value="{_xml_escape(value)}" -->\n')

    root_share_path = Path("launch") / launcher

    # Stack of currently open source-level <group> boundaries.
    open_src: list[tuple[str, Path]] = []

    for action in actions:
        # Compute the target source stack.
        full_stack = _action_source_stack(action, package, root_share_path)
        target_src = full_stack

        common = _common_prefix_len(open_src, target_src)

        # Close excess source groups, innermost first.
        while len(open_src) > common:
            depth = len(open_src) - 1
            pkg, path = open_src.pop()
            vd = _visual_src_depth(depth)
            out.append(f"{_pad(vd)}</group>\n")
            out.append(f"{_pad(vd)}<!-- end: {_format_source_label(pkg, path)} -->\n")

        # Open new source groups, outermost first.
        while len(open_src) < len(target_src):
            depth = len(open_src)
            vd = _visual_src_depth(depth)
            pkg, path = target_src[depth]
            out.append(f"{_pad(vd)}<!-- source: {_format_source_label(pkg, path)} -->\n")
            out.append(f"{_pad(vd)}<group>\n")
            if show_args:
                _render_show_args((pkg, path), include_args, declared_args_by_file, vd + 1, out)
            open_src.append((pkg, path))

        # Render with correct indentation.
        vd = _visual_src_depth(len(open_src))
        indent = _pad(vd)
        snippet = action.serialize_resolved(indent)
        if snippet is not None:
            out.append(snippet)

    # Close remaining source groups.
    while open_src:
        depth = len(open_src) - 1
        pkg, path = open_src.pop()
        vd = _visual_src_depth(depth)
        out.append(f"{_pad(vd)}</group>\n")
        out.append(f"{_pad(vd)}<!-- end: {_format_source_label(pkg, path)} -->\n")

    out.append("</launch>\n")
    return "".join(out)
