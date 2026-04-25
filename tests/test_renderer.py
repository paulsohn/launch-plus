"""Tests for roscope.renderer — action-based rendering."""

from __future__ import annotations

from roscope.entities.actions.composable_node_container import ComposableNodeContainer
from roscope.entities.actions.execute_process import ExecuteProcess
from roscope.entities.actions.group_action import GroupAction
from roscope.entities.actions.load_composable_nodes import LoadComposableNodes
from roscope.entities.actions.marker import SourceMarker
from roscope.entities.actions.node import Node
from roscope.entities.descriptions import ComposableNode
from roscope.entities.launch_context import LaunchContext, ResolverState
from roscope.renderer import render_resolved_xml


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
    assert 'target="/my_container"' in xml


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


def test_executable_env_children() -> None:
    ctx = _make_ctx()
    ep = ExecuteProcess(cmd=["echo"], additional_env={"ROSCOPE_TEST_VAR": "hello"})
    resolved = ep.execute(ctx)
    elems = resolved[0].serialize_resolved()
    assert len(elems) == 1
    env_map = {e.get("name"): e.get("value") for e in elems[0].findall("env")}
    assert env_map.get("ROSCOPE_TEST_VAR") == "hello"


def test_node_env_children() -> None:
    ctx = _make_ctx()
    node = Node(package="p", executable="e", additional_env={"ROSCOPE_TEST_VAR": "world"})
    resolved = node.execute(ctx)
    elems = resolved[0].serialize_resolved()
    assert len(elems) == 1
    env_elems = elems[0].findall("env")
    # Regression: env must not appear twice (duplicate loop bug)
    env_names = [e.get("name") for e in env_elems]
    assert env_names.count("ROSCOPE_TEST_VAR") == 1
    env_map = {e.get("name"): e.get("value") for e in env_elems}
    assert env_map.get("ROSCOPE_TEST_VAR") == "world"


# ─── Source group nesting ────────────────────────────────────────────────────


def test_groups_by_source_file() -> None:
    ctx = _make_ctx()
    r1 = Node(package="p1", executable="e1").execute(ctx)
    r2 = Node(package="p2", executable="e2").execute(ctx)

    actions = [
        GroupAction(
            resolved_children=[
                SourceMarker("/install/share/sensor_launch/launch/sensing.launch.xml"),
                *r1,
                *r2,
            ]
        ),
    ]
    xml = render_resolved_xml("my_pkg", "top.launch.xml", actions)
    assert xml.count("<group>") == 1
    assert xml.count("</group>") == 1
    assert "source: /install/share/sensor_launch/launch/sensing.launch.xml" in xml


def test_nested_groups() -> None:
    ctx = _make_ctx()
    resolved_n = Node(package="p", executable="e", name="n").execute(ctx)

    inner = GroupAction(
        resolved_children=[
            SourceMarker("/install/share/sensing_pkg/launch/sensing.launch.xml"),
            *resolved_n,
        ]
    )
    mid = GroupAction(
        resolved_children=[
            SourceMarker("/install/share/comp_pkg/launch/comp.launch.xml"),
            inner,
        ]
    )
    actions = [
        GroupAction(
            resolved_children=[
                SourceMarker("/install/share/root_pkg/launch/root.launch.xml"),
                mid,
            ]
        ),
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
