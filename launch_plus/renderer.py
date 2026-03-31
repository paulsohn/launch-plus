"""Render resolved launch nodes as XML.

Ported from crates/launch-plus-core/src/resolver.rs (Renderer section).
"""

from __future__ import annotations

import logging
from pathlib import Path

from launch_plus.types import (
    ComposablePlugin,
    IncludeArgContext,
    NodeKindTag,
    ParamFileInlined,
    ParamFileReference,
    ResolvedNode,
)

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
    """Indentation string for the given visual depth level.

    ``_pad(0)`` = ``"  "`` (2 spaces, content at ``<launch>`` level).
    ``_pad(d)`` = ``"  " * (d + 1)``.
    """
    return "  " * (depth + 1)


def _visual_src_depth(open_src_len: int) -> int:
    """Visual source depth — drives indentation."""
    return open_src_len


def _format_source_label(pkg: str, path: Path) -> str:
    """Format a source label for XML comments.

    Returns ``pkg://path`` when the package is non-empty, or the bare path
    when the package is empty.
    """
    if not pkg:
        return str(path)
    return f"{pkg}://{path}"


def _node_source_stack(
    node: ResolvedNode,
    root_pkg: str,
    root_path: Path,
) -> list[tuple[str, Path]]:
    """Compute the target source-group nesting stack for a node."""
    if node.include_chain:
        chain = node.include_chain
        first_pkg, first_path = chain[0]
        if first_pkg == root_pkg and first_path == root_path:
            return list(chain[1:])
        return list(chain)
    # Inline mode (unit tests without orchestrator context).
    if node.source is not None:
        pkg, path = node.source
        if pkg == root_pkg and path == root_path:
            return []
        return [(pkg, path)]
    return []


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


# ---------------------------------------------------------------------------
# Render individual elements
# ---------------------------------------------------------------------------


def _render_param_file(pf: ParamFileReference | ParamFileInlined, ind: str, out: list[str]) -> None:
    if isinstance(pf, ParamFileReference):
        out.append(f'{ind}<param from="{_xml_escape(pf.display)}"/>\n')
    elif isinstance(pf, ParamFileInlined):
        out.append(f"{ind}<!-- params from: {_xml_escape(pf.display)} -->\n")
        for name, value in pf.params:
            out.append(f'{ind}<param name="{_xml_escape(name)}" value="{_xml_escape(value)}"/>\n')
        out.append(f"{ind}<!-- end params from: {_xml_escape(pf.display)} -->\n")


def _render_node_common_attrs(node: ResolvedNode, stack_only_ns: str | None) -> str:
    """Build the common attribute string for node/lifecycle_node/node_container tags."""
    parts: list[str] = []
    if node.name is not None:
        parts.append(f' name="{_xml_escape(node.name)}"')
    # Emit namespace= only when not fully covered by enclosing <push-ros-namespace>.
    if node.namespace != stack_only_ns and node.namespace is not None:
        parts.append(f' namespace="{_xml_escape(node.namespace)}"')
    if node.output is not None:
        parts.append(f' output="{_xml_escape(node.output)}"')
    if node.args is not None:
        parts.append(f' args="{_xml_escape(node.args)}"')
    if node.respawn is not None:
        parts.append(f' respawn="{_xml_escape(node.respawn)}"')
    if node.respawn_delay is not None:
        parts.append(f' respawn_delay="{_xml_escape(node.respawn_delay)}"')
    return "".join(parts)


def _render_node_children(
    node: ResolvedNode,
    child_ind: str,
    out: list[str],
) -> None:
    """Render param_files, parameters, remappings, env children."""
    for pf in node.param_files:
        _render_param_file(pf, child_ind, out)
    for key, value in sorted(node.parameters.items()):
        out.append(f'{child_ind}<param name="{_xml_escape(key)}" value="{_xml_escape(value)}"/>\n')
    for from_, to in node.remappings:
        # Only qualify 'to' — 'from' is a node-internal name pattern, not a graph topic
        to = _qualify_topic(to, node.namespace)
        out.append(f'{child_ind}<remap from="{_xml_escape(from_)}" to="{_xml_escape(to)}"/>\n')
    for name, value in sorted(node.env.items()):
        out.append(f'{child_ind}<env name="{_xml_escape(name)}" value="{_xml_escape(value)}"/>\n')


def _has_node_children(node: ResolvedNode) -> bool:
    return bool(node.param_files or node.parameters or node.remappings or node.env)


def _render_node(
    node: ResolvedNode,
    node_ind: str,
    child_ind: str,
    stack_only_ns: str | None,
    out: list[str],
) -> None:
    """Emit a single ``<node>`` element."""
    pkg_esc = _xml_escape(node.package)
    exec_esc = _xml_escape(node.executable)
    tag = f'{node_ind}<node pkg="{pkg_esc}" exec="{exec_esc}"'
    tag += _render_node_common_attrs(node, stack_only_ns)
    if _has_node_children(node):
        out.append(f"{tag}>\n")
        _render_node_children(node, child_ind, out)
        out.append(f"{node_ind}</node>\n")
    else:
        out.append(f"{tag}/>\n")


def _render_lifecycle_node(
    node: ResolvedNode,
    node_ind: str,
    child_ind: str,
    stack_only_ns: str | None,
    out: list[str],
) -> None:
    """Emit a ``<lifecycle_node>`` element."""
    pkg_esc = _xml_escape(node.package)
    exec_esc = _xml_escape(node.executable)
    tag = f'{node_ind}<lifecycle_node pkg="{pkg_esc}" exec="{exec_esc}"'
    tag += _render_node_common_attrs(node, stack_only_ns)
    if _has_node_children(node):
        out.append(f"{tag}>\n")
        _render_node_children(node, child_ind, out)
        out.append(f"{node_ind}</lifecycle_node>\n")
    else:
        out.append(f"{tag}/>\n")


def _render_composable_plugin(plugin: ComposablePlugin, ind: str, out: list[str]) -> None:
    """Emit a single ``<composable_node>`` element."""
    child_ind = ind + "  "
    pkg_esc = _xml_escape(plugin.package)
    plugin_esc = _xml_escape(plugin.plugin)
    tag = f'{ind}<composable_node pkg="{pkg_esc}" plugin="{plugin_esc}"'
    if plugin.name is not None:
        tag += f' name="{_xml_escape(plugin.name)}"'
    has_children = bool(plugin.param_files or plugin.parameters or plugin.remappings)
    if has_children:
        out.append(f"{tag}>\n")
        for pf in plugin.param_files:
            _render_param_file(pf, child_ind, out)
        for key, value in sorted(plugin.parameters.items()):
            out.append(
                f'{child_ind}<param name="{_xml_escape(key)}" value="{_xml_escape(value)}"/>\n'
            )
        for from_, to in plugin.remappings:
            out.append(f'{child_ind}<remap from="{_xml_escape(from_)}" to="{_xml_escape(to)}"/>\n')
        out.append(f"{ind}</composable_node>\n")
    else:
        out.append(f"{tag}/>\n")


def _render_container_node(
    node: ResolvedNode,
    node_ind: str,
    child_ind: str,
    stack_only_ns: str | None,
    out: list[str],
) -> None:
    """Emit a ``<node_container>`` element with nested ``<composable_node>`` children."""
    pkg_esc = _xml_escape(node.package)
    exec_esc = _xml_escape(node.executable)
    tag = f'{node_ind}<node_container pkg="{pkg_esc}" exec="{exec_esc}"'
    tag += _render_node_common_attrs(node, stack_only_ns)
    has_children = bool(
        node.plugins or node.param_files or node.parameters or node.remappings or node.env
    )
    if not has_children:
        out.append(f"{tag}/>\n")
    else:
        out.append(f"{tag}>\n")
        _render_node_children(node, child_ind, out)
        for plugin in node.plugins:
            _render_composable_plugin(plugin, child_ind, out)
        out.append(f"{node_ind}</node_container>\n")


def _render_load_composable_node(
    node: ResolvedNode,
    node_ind: str,
    child_ind: str,
    out: list[str],
) -> None:
    """Emit a ``<load_composable_node>`` element."""
    if not node.plugins:
        return
    tag = f"{node_ind}<load_composable_node"
    target = node.load_target or ""
    if target:
        tag += f' target="{_xml_escape(target)}"'
    out.append(f"{tag}>\n")
    for plugin in node.plugins:
        _render_composable_plugin(plugin, child_ind, out)
    out.append(f"{node_ind}</load_composable_node>\n")


def _render_event_handler(
    node: ResolvedNode,
    node_ind: str,
    child_ind: str,
    out: list[str],
) -> None:
    """Emit an event handler element."""
    assert node.handler_kind is not None
    tag_name = node.handler_kind.tag_name
    tag = f"{node_ind}<{tag_name}"
    if node.handler_target is not None:
        tag += f' target="{_xml_escape(node.handler_target)}"'
    if node.handler_target_node is not None:
        tag += f' target_node="{_xml_escape(node.handler_target_node)}"'
    if node.handler_namespace is not None:
        tag += f' namespace="{_xml_escape(node.handler_namespace)}"'
    if node.handler_start_state is not None:
        tag += f' start_state="{_xml_escape(node.handler_start_state)}"'
    if node.handler_goal_state is not None:
        tag += f' goal_state="{_xml_escape(node.handler_goal_state)}"'

    if not node.handler_actions:
        out.append(f"{tag}/>\n")
    else:
        out.append(f"{tag}>\n")
        for action in node.handler_actions:
            tn_attr = ""
            if action.target_node is not None:
                tn_attr = f' target_node="{_xml_escape(action.target_node)}"'
            ns_attr = ""
            if action.namespace is not None:
                ns_attr = f' namespace="{_xml_escape(action.namespace)}"'
            out.append(
                f'{child_ind}<emit_event event="{_xml_escape(action.event)}"{tn_attr}{ns_attr}/>\n'
            )
        out.append(f"{node_ind}</{tag_name}>\n")


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

    # Merge: explicit args take precedence, then declared defaults
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
# Main entry point
# ---------------------------------------------------------------------------


def render_resolved_xml(
    package: str,
    launcher: str,
    nodes: list[ResolvedNode],
    *,
    include_args: dict[tuple[str, Path], IncludeArgContext] | None = None,
    show_args: bool = False,
    initial_args: dict[str, str] | None = None,
    declared_args_by_file: dict[tuple[str, Path], dict[str, str]] | None = None,
) -> str:
    """Render resolved nodes as a ``<launch>`` XML document.

    Each source file boundary produces a nested ``<group>`` element.
    Namespaces are flattened: the fully composed ``namespace=`` value
    is emitted directly on each ``<node>`` element.
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

    # Ordering check: warn when a LoadComposable targets a container not yet seen.
    seen_containers: set[str] = set()
    for node in nodes:
        if node.kind == NodeKindTag.CONTAINER:
            if node.name is not None:
                seen_containers.add(node.name)
                if node.namespace is not None:
                    seen_containers.add(f"{node.namespace.rstrip('/')}/{node.name}")
        elif node.kind == NodeKindTag.LOAD_COMPOSABLE:
            target = node.load_target or ""
            if target:
                found = any(
                    c == target or c.endswith(f"/{target}") or target.endswith(f"/{c}")
                    for c in seen_containers
                )
                if not found:
                    logger.warning(
                        "load_composable_node targets '%s' which does not appear before it "
                        "in the resolved output; launch may fail at runtime if the container "
                        "is not yet running",
                        target,
                    )

    # Stack of currently open source-level <group> boundaries.
    open_src: list[tuple[str, Path]] = []

    for node in nodes:
        # Compute the target source stack.
        full_stack = _node_source_stack(node, package, root_share_path)
        target_src = full_stack

        common = _common_prefix_len(open_src, target_src)
        src_changing = common < len(open_src) or len(open_src) < len(target_src)

        # IncludeMarker handling.
        if node.kind == NodeKindTag.INCLUDE_MARKER:
            # Close excess source groups.
            while len(open_src) > common:
                depth = len(open_src) - 1
                pkg, path = open_src.pop()
                vd = _visual_src_depth(depth)
                out.append(f"{_pad(vd)}</group>\n")
                out.append(f"{_pad(vd)}<!-- end: {_format_source_label(pkg, path)} -->\n")

            # Open intermediate levels persistently.
            while len(open_src) + 1 < len(target_src):
                depth = len(open_src)
                vd = _visual_src_depth(depth)
                pkg, path = target_src[depth]
                out.append(f"{_pad(vd)}<!-- source: {_format_source_label(pkg, path)} -->\n")
                out.append(f"{_pad(vd)}<group>\n")
                if show_args:
                    _render_show_args((pkg, path), include_args, declared_args_by_file, vd + 1, out)
                open_src.append((pkg, path))

            # The marker's own level: inline comment pair only.
            if len(open_src) < len(target_src):
                depth = len(open_src)
                vd = _visual_src_depth(depth)
                pkg, path = target_src[depth]
                out.append(f"{_pad(vd)}<!-- source: {_format_source_label(pkg, path)} -->\n")
                if show_args:
                    _render_show_args((pkg, path), include_args, declared_args_by_file, vd + 1, out)
                out.append(f"{_pad(vd)}<!-- end: {_format_source_label(pkg, path)} -->\n")
            continue

        if src_changing:
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

        # Compute node indentation.  Namespaces are always flattened onto
        # node attributes, so there is no namespace sub-group.
        vd = _visual_src_depth(len(open_src))
        node_ind = _pad(vd)
        child_ind = _pad(vd + 1)
        stack_only_ns: str | None = None

        # Render by node kind.
        if node.kind == NodeKindTag.NODE:
            _render_node(node, node_ind, child_ind, stack_only_ns, out)
        elif node.kind == NodeKindTag.CONTAINER:
            _render_container_node(node, node_ind, child_ind, stack_only_ns, out)
        elif node.kind == NodeKindTag.LOAD_COMPOSABLE:
            _render_load_composable_node(node, node_ind, child_ind, out)
        elif node.kind == NodeKindTag.LOG:
            msg = node.log_message or ""
            out.append(f'{node_ind}<log message="{_xml_escape(msg)}"/>\n')
        elif node.kind == NodeKindTag.SET_REMAP:
            from_ = node.remap_from or ""
            to = node.remap_to or ""
            out.append(
                f'{node_ind}<set_remap from="{_xml_escape(from_)}" to="{_xml_escape(to)}"/>\n'
            )
        elif node.kind == NodeKindTag.EXECUTABLE:
            cmd = node.exec_cmd or ""
            name_attr = ""
            if node.exec_name is not None:
                name_attr = f' name="{_xml_escape(node.exec_name)}"'
            shell = str(node.exec_shell).lower()
            out.append(
                f'{node_ind}<executable cmd="{_xml_escape(cmd)}"{name_attr} shell="{shell}"/>\n'
            )
        elif node.kind == NodeKindTag.LIFECYCLE_NODE:
            _render_lifecycle_node(node, node_ind, child_ind, stack_only_ns, out)
        elif node.kind == NodeKindTag.EVENT_HANDLER:
            _render_event_handler(node, node_ind, child_ind, out)

    # Cleanup: no namespace sub-group to close (namespaces always flattened).

    # Close remaining source groups, innermost first.
    while open_src:
        depth = len(open_src) - 1
        pkg, path = open_src.pop()
        vd = _visual_src_depth(depth)
        out.append(f"{_pad(vd)}</group>\n")
        out.append(f"{_pad(vd)}<!-- end: {_format_source_label(pkg, path)} -->\n")

    out.append("</launch>\n")
    return "".join(out)
