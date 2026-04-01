"""Tests for launch_plus.renderer — action-based rendering."""

from __future__ import annotations

from launch_plus.entities.actions.executable import ExecuteProcess
from launch_plus.entities.actions.marker import EndSourceMarker, SourceMarker
from launch_plus.entities.actions.node import (
    ComposableNode,
    ComposableNodeContainer,
    LoadComposableNodes,
    Node,
)
from launch_plus.entities.state import LaunchContext, ResolverState
from launch_plus.renderer import render_resolved_xml


def _make_ctx(**lc):
    state = ResolverState()
    ctx = LaunchContext(state)
    ctx._launch_configurations.update(lc)
    return ctx


def _resolve_and_render(actions, *, show_args=False, initial_args=None):
    """Helper: render a list of already-executed actions."""
    return render_resolved_xml(
        "my_pkg",
        "top.launch.xml",
        actions,
        show_args=show_args,
        initial_args=initial_args,
    )


# ─── Node rendering ─────────────────────────────────────────────────────────


def test_node_basic() -> None:
    ctx = _make_ctx()
    node = Node(package="my_pkg", executable="my_exec", name="my_node")
    resolved = node.execute(ctx)
    xml = render_resolved_xml("p", "l.xml", resolved)
    assert '<node pkg="my_pkg" exec="my_exec" name="my_node"/>' in xml


def test_node_with_params() -> None:
    ctx = _make_ctx(global_params=[("gp", "gv")])
    node = Node(package="p", executable="e", name="n")
    resolved = node.execute(ctx)
    xml = render_resolved_xml("p", "l.xml", resolved)
    assert '<param name="gp" value="gv"/>' in xml


def test_node_with_namespace() -> None:
    ctx = _make_ctx(ros_namespace="/my_ns")
    node = Node(package="p", executable="e", name="n")
    resolved = node.execute(ctx)
    xml = render_resolved_xml("p", "l.xml", resolved)
    assert 'namespace="/my_ns"' in xml


def test_node_escapes_quotes() -> None:
    import xml.etree.ElementTree as ET

    ctx = _make_ctx(global_params=[("key", 'val with "quotes"')])
    node = Node(package="p", executable="e", name="n")
    resolved = node.execute(ctx)
    elems = resolved[0].serialize_resolved()
    assert len(elems) == 1
    snippet = ET.tostring(elems[0], encoding="unicode")
    assert "&quot;" in snippet
    assert 'val with "quotes"' not in snippet


# ─── Container rendering ────────────────────────────────────────────────────


def test_container_with_plugins() -> None:
    ctx = _make_ctx()
    desc = ComposableNode(package="comp_pkg", plugin="comp_pkg::MyPlugin", name="my_comp")
    container = ComposableNodeContainer(
        package="rclcpp_components",
        executable="component_container",
        name="my_container",
        composable_node_descriptions=[desc],
    )
    resolved = container.execute(ctx)
    xml = render_resolved_xml("p", "l.xml", resolved)
    assert "<node_container" in xml
    assert "<composable_node" in xml
    assert 'plugin="comp_pkg::MyPlugin"' in xml


# ─── LoadComposableNodes rendering ──────────────────────────────────────────


def test_load_composable() -> None:
    ctx = _make_ctx()
    desc = ComposableNode(package="comp_pkg", plugin="comp_pkg::Node", name="comp")
    load = LoadComposableNodes(
        composable_node_descriptions=[desc],
        target_container="my_container",
    )
    resolved = load.execute(ctx)
    xml = render_resolved_xml("p", "l.xml", resolved)
    assert "<load_composable_node" in xml
    assert 'target="my_container"' in xml


# ─── ExecuteProcess rendering ───────────────────────────────────────────────


def test_executable() -> None:
    import xml.etree.ElementTree as ET

    ctx = _make_ctx()
    ep = ExecuteProcess(cmd=["echo", "hello"])
    resolved = ep.execute(ctx)
    elems = resolved[0].serialize_resolved()
    assert len(elems) == 1
    snippet = ET.tostring(elems[0], encoding="unicode")
    assert "<executable" in snippet
    assert "echo hello" in snippet


# ─── Source group nesting ────────────────────────────────────────────────────


def test_groups_by_source_file() -> None:
    ctx = _make_ctx()
    r1 = Node(package="p1", executable="e1").execute(ctx)
    r2 = Node(package="p2", executable="e2").execute(ctx)

    actions = [
        SourceMarker("sensor_launch", "launch/sensing.launch.xml"),
        *r1,
        *r2,
        EndSourceMarker("sensor_launch", "launch/sensing.launch.xml"),
    ]
    xml = render_resolved_xml("my_pkg", "top.launch.xml", actions)
    assert xml.count("<group>") == 1
    assert xml.count("</group>") == 1
    assert "sensor_launch://launch/sensing.launch.xml" in xml


def test_nested_groups() -> None:
    ctx = _make_ctx()
    resolved_n = Node(package="p", executable="e", name="n").execute(ctx)

    actions = [
        SourceMarker("root_pkg", "launch/root.launch.xml"),
        SourceMarker("comp_pkg", "launch/comp.launch.xml"),
        SourceMarker("sensing_pkg", "launch/sensing.launch.xml"),
        *resolved_n,
        EndSourceMarker("sensing_pkg", "launch/sensing.launch.xml"),
        EndSourceMarker("comp_pkg", "launch/comp.launch.xml"),
        EndSourceMarker("root_pkg", "launch/root.launch.xml"),
    ]
    xml = render_resolved_xml("my_pkg", "top.launch.xml", actions)
    assert xml.count("<group>") == 3
    assert xml.count("</group>") == 3


# ─── Show args ──────────────────────────────────────────────────────────────


def test_show_args() -> None:
    ctx = _make_ctx()
    resolved = Node(package="p", executable="e").execute(ctx)

    xml = render_resolved_xml(
        "my_pkg",
        "top.launch.xml",
        resolved,
        show_args=True,
        initial_args={"vehicle_model": "sample", "sensor_model": "kit"},
    )
    assert '<!-- arg name="sensor_model" value="kit" -->' in xml
    assert '<!-- arg name="vehicle_model" value="sample" -->' in xml


# ─── XML escape ─────────────────────────────────────────────────────────────


def test_xml_escape() -> None:
    from launch_plus.renderer import _xml_escape

    assert _xml_escape('a "b" c') == "a &quot;b&quot; c"
    assert _xml_escape("a & b") == "a &amp; b"
    assert _xml_escape("a < b > c") == "a &lt; b &gt; c"
