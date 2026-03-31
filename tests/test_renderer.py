"""Tests for launch_plus.renderer — ported from resolver.rs Rust tests."""

from __future__ import annotations

from pathlib import Path

from launch_plus.renderer import render_resolved_xml
from launch_plus.types import NodeKindTag, ResolvedNode


def test_groups_by_source_file() -> None:
    """Two nodes from the same source file -> one <group>.
    One node without a source file -> emitted flat at <launch> level.
    """
    src_a = ("sensor_launch", Path("launch/sensing.launch.xml"))
    src_b = ("planner_launch", Path("launch/planning.launch.xml"))

    nodes = [
        ResolvedNode(
            package="sensor_pkg",
            executable="sensor_node",
            name="lidar",
            source=src_a,
            parameters={"rate": "10"},
        ),
        ResolvedNode(
            package="sensor_pkg",
            executable="camera_node",
            name="cam",
            source=src_a,
            remappings=[("/in", "/out")],
        ),
        ResolvedNode(
            package="planner_pkg",
            executable="planner",
            source=src_b,
        ),
        ResolvedNode(
            package="util_pkg",
            executable="util",
            # no source -> flat
        ),
    ]

    xml = render_resolved_xml("my_pkg", "launcher.launch.xml", nodes)

    # Two <group> elements (one per source file)
    assert xml.count("<group>") == 2, f"expected 2 groups\n{xml}"
    assert xml.count("</group>") == 2

    # sensing group: sensor_node and camera_node both inside
    sensing_start = xml.index("sensor_launch://launch/sensing.launch.xml")
    sensing_end = xml.index("</group>", sensing_start)
    sensing_group = xml[sensing_start:sensing_end]
    assert "sensor_node" in sensing_group
    assert "camera_node" in sensing_group
    assert "      <param" in sensing_group, "param should be 6-space indented"
    assert "      <remap" in sensing_group, "remap should be 6-space indented"

    # planning group
    assert "planner_launch://launch/planning.launch.xml" in xml
    assert "planner_pkg" in xml

    # util_pkg (no source) should be flat at 2-space indent
    util_idx = xml.index("util_pkg")
    line_start = xml.rfind("\n", 0, util_idx) + 1
    line = xml[line_start : util_idx + len("util_pkg")]
    assert line.startswith("  <node"), f"ungrouped node should be at 2-space indent, got: {line!r}"


def test_nested_groups_from_include_chain() -> None:
    """Nodes with include_chain set produce nested <group>s."""
    root = ("my_pkg", Path("launch/root.launch.xml"))
    comp = ("comp_pkg", Path("launch/comp.launch.xml"))
    sensing = ("sensing_pkg", Path("launch/sensing.launch.xml"))

    nodes = [
        ResolvedNode(
            package="lidar_pkg",
            executable="lidar_node",
            name="lidar",
            source=sensing,
            include_chain=[root, comp, sensing],
        ),
        ResolvedNode(
            package="radar_pkg",
            executable="radar_node",
            source=sensing,
            include_chain=[root, comp, sensing],
        ),
    ]

    xml = render_resolved_xml("my_pkg", "root.launch.xml", nodes)

    assert xml.count("<group>") == 2, f"expected 2 nested groups\n{xml}"
    assert xml.count("</group>") == 2
    assert "lidar_node" in xml
    assert "radar_node" in xml

    comp_pos = xml.index("comp_pkg://launch/comp.launch.xml")
    sensing_pos = xml.index("sensing_pkg://launch/sensing.launch.xml")
    assert comp_pos < sensing_pos

    outer_group_pos = xml.index("<group>")
    assert sensing_pos > outer_group_pos

    lidar_line = next(line for line in xml.splitlines() if "lidar_node" in line)
    assert lidar_line.startswith("      "), (
        f"lidar_node should be at 6-space indent: {lidar_line!r}"
    )

    assert xml.count("<!-- end:") == 2


def test_include_marker_inline_comment_pair_no_group() -> None:
    """IncludeMarker emits source/end comments without a <group>."""
    nodes = [
        ResolvedNode(
            package="parent_pkg",
            executable="parent_node",
            name="parent",
            source=("root_pkg", Path("launch/root.launch.xml")),
            include_chain=[("root_pkg", Path("launch/root.launch.xml"))],
        ),
        ResolvedNode(
            kind=NodeKindTag.INCLUDE_MARKER,
            source=("preset_pkg", Path("launch/preset.launch.xml")),
            include_chain=[
                ("root_pkg", Path("launch/root.launch.xml")),
                ("preset_pkg", Path("launch/preset.launch.xml")),
            ],
        ),
    ]

    xml = render_resolved_xml("root_pkg", "root.launch.xml", nodes)

    assert "<!-- source: preset_pkg://launch/preset.launch.xml -->" in xml
    assert "<!-- end: preset_pkg://launch/preset.launch.xml -->" in xml
    assert "<group>" not in xml


def test_container_with_plugins_and_params() -> None:
    """Container node renders params, remaps, env, and plugins."""
    from launch_plus.types import ComposablePlugin

    nodes = [
        ResolvedNode(
            package="container_pkg",
            executable="component_container",
            name="my_container",
            kind=NodeKindTag.CONTAINER,
            parameters={"use_sim_time": "true"},
            env={"ROS_LOG_DIR": "/tmp"},
            plugins=[
                ComposablePlugin(
                    package="plugin_pkg",
                    plugin="plugin_pkg::MyPlugin",
                    name="my_plugin",
                    parameters={"rate": "10"},
                ),
            ],
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert "<node_container" in xml
    assert 'name="my_container"' in xml
    assert '<param name="use_sim_time" value="true"/>' in xml
    assert '<env name="ROS_LOG_DIR" value="/tmp"/>' in xml
    assert "<composable_node" in xml
    assert 'plugin="plugin_pkg::MyPlugin"' in xml
    assert "</node_container>" in xml


def test_load_composable_node() -> None:
    """LoadComposable renders target and plugins."""
    from launch_plus.types import ComposablePlugin

    nodes = [
        ResolvedNode(
            kind=NodeKindTag.LOAD_COMPOSABLE,
            load_target="my_container",
            plugins=[
                ComposablePlugin(
                    package="plugin_pkg",
                    plugin="plugin_pkg::Sensor",
                    name="sensor",
                ),
            ],
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert '<load_composable_node target="my_container">' in xml
    assert 'plugin="plugin_pkg::Sensor"' in xml
    assert "</load_composable_node>" in xml


def test_event_handler() -> None:
    """Event handler renders correctly."""
    from launch_plus.types import EventHandlerKind, ResolvedEventAction

    nodes = [
        ResolvedNode(
            kind=NodeKindTag.EVENT_HANDLER,
            handler_kind=EventHandlerKind.ON_PROCESS_EXIT,
            handler_target="my_node",
            handler_actions=[
                ResolvedEventAction(event="shutdown"),
            ],
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert '<on_process_exit target="my_node">' in xml
    assert '<emit_event event="shutdown"/>' in xml
    assert "</on_process_exit>" in xml


def test_log_and_set_remap() -> None:
    """Log and SetRemap node kinds render correctly."""
    nodes = [
        ResolvedNode(
            kind=NodeKindTag.LOG,
            log_message="Starting system",
        ),
        ResolvedNode(
            kind=NodeKindTag.SET_REMAP,
            remap_from="/input",
            remap_to="/output",
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert '<log message="Starting system"/>' in xml
    assert '<set_remap from="/input" to="/output"/>' in xml


def test_executable() -> None:
    """Executable node kind renders correctly."""
    nodes = [
        ResolvedNode(
            kind=NodeKindTag.EXECUTABLE,
            exec_cmd="ros2 bag play /data/bag",
            exec_name="bag_player",
            exec_shell=True,
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert '<executable cmd="ros2 bag play /data/bag" name="bag_player" shell="true"/>' in xml


def test_lifecycle_node() -> None:
    """Lifecycle node renders with lifecycle_node tag."""
    nodes = [
        ResolvedNode(
            package="lifecycle_pkg",
            executable="managed_node",
            name="my_lifecycle",
            kind=NodeKindTag.LIFECYCLE_NODE,
            parameters={"autostart": "true"},
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert "<lifecycle_node" in xml
    assert 'name="my_lifecycle"' in xml
    assert '<param name="autostart" value="true"/>' in xml
    assert "</lifecycle_node>" in xml


def test_xml_escape() -> None:
    """Special characters in attribute values are escaped."""
    nodes = [
        ResolvedNode(
            package="pkg",
            executable="node",
            name='a<b>&"c',
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert 'name="a&lt;b&gt;&amp;&quot;c"' in xml


def test_show_args() -> None:
    """show_args renders initial args at top and include args at boundaries."""
    from launch_plus.types import IncludeArgContext

    src = ("child_pkg", Path("launch/child.launch.xml"))

    nodes = [
        ResolvedNode(
            package="child_pkg",
            executable="child_node",
            source=src,
        ),
    ]

    include_args = {
        src: IncludeArgContext(explicit={"model": "robot_a"}),
    }
    declared = {
        src: {"model": "default_model", "rate": "10"},
    }

    xml = render_resolved_xml(
        "my_pkg",
        "top.launch.xml",
        nodes,
        show_args=True,
        initial_args={"vehicle": "car"},
        include_args=include_args,
        declared_args_by_file=declared,
    )

    # Initial arg at top
    assert '<!-- arg name="vehicle" value="car" -->' in xml
    # Explicit arg at include boundary (value, not default)
    assert '<!-- arg name="model" value="robot_a" -->' in xml
    # Declared default (not explicit) at include boundary
    assert '<!-- arg name="rate" default="10" -->' in xml


def test_param_file_reference_and_inlined() -> None:
    """ParamFile rendering — both Reference and Inlined."""
    from launch_plus.types import ParamFileInlined, ParamFileReference

    nodes = [
        ResolvedNode(
            package="pkg",
            executable="node",
            param_files=[
                ParamFileReference(display="config/params.yaml", abs="/ws/config/params.yaml"),
                ParamFileInlined(
                    display="config/inline.yaml",
                    params=[("key1", "val1"), ("key2", "val2")],
                ),
            ],
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert '<param from="config/params.yaml"/>' in xml
    assert "<!-- params from: config/inline.yaml -->" in xml
    assert '<param name="key1" value="val1"/>' in xml
    assert '<param name="key2" value="val2"/>' in xml
    assert "<!-- end params from: config/inline.yaml -->" in xml


def test_format_source_label_empty_package() -> None:
    """Empty package -> bare path, not pkg://path."""
    nodes = [
        ResolvedNode(
            package="child",
            executable="node",
            source=("", Path("/absolute/path/launch.xml")),
        ),
    ]

    xml = render_resolved_xml("my_pkg", "top.launch.xml", nodes)

    assert "<!-- source: /absolute/path/launch.xml -->" in xml
    assert "<!-- end: /absolute/path/launch.xml -->" in xml
    # Must NOT contain ":///"
    assert ":///" not in xml
