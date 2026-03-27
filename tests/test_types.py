"""Tests for launch_plus.types — IR dataclasses and namespace helpers."""

from pathlib import Path

from launch_plus.types import (
    ComposablePlugin,
    DependencyKind,
    EventHandlerKind,
    FileDependency,
    IncludeArgContext,
    LaunchInclude,
    NodeKindTag,
    ParamFileInlined,
    ParamFileReference,
    ParsedLaunchFile,
    ResolvedEventAction,
    ResolvedNode,
    SemanticNode,
    effective_namespace,
    ros2_namespace_join,
    semantic_eq,
)

# ---------------------------------------------------------------------------
# ros2_namespace_join
# ---------------------------------------------------------------------------


class TestRos2NamespaceJoin:
    def test_relative_onto_none(self) -> None:
        assert ros2_namespace_join(None, "sensing") == "/sensing"

    def test_relative_onto_empty(self) -> None:
        assert ros2_namespace_join("", "sensing") == "/sensing"

    def test_relative_onto_root(self) -> None:
        assert ros2_namespace_join("/", "sensing") == "/sensing"

    def test_relative_onto_existing(self) -> None:
        assert ros2_namespace_join("/sensing", "lidar") == "/sensing/lidar"

    def test_absolute_resets(self) -> None:
        assert ros2_namespace_join("/sensing", "/abs") == "/abs"

    def test_empty_next_preserves_base(self) -> None:
        assert ros2_namespace_join("/sensing", "") == "/sensing"

    def test_empty_next_preserves_none(self) -> None:
        assert ros2_namespace_join(None, "") is None

    def test_trailing_slash_stripped(self) -> None:
        assert ros2_namespace_join(None, "sensing/") == "/sensing"

    def test_base_trailing_slash_stripped(self) -> None:
        assert ros2_namespace_join("/sensing/", "lidar") == "/sensing/lidar"


# ---------------------------------------------------------------------------
# effective_namespace
# ---------------------------------------------------------------------------


class TestEffectiveNamespace:
    def test_empty_stack_no_ns(self) -> None:
        assert effective_namespace([]) is None

    def test_empty_stack_with_ns(self) -> None:
        assert effective_namespace([], "robot") == "/robot"

    def test_single_relative(self) -> None:
        assert effective_namespace(["sensing"]) == "/sensing"

    def test_multiple_relative(self) -> None:
        assert effective_namespace(["sensing", "lidar"]) == "/sensing/lidar"

    def test_absolute_resets_stack(self) -> None:
        assert effective_namespace(["sensing", "/abs"]) == "/abs"

    def test_stack_plus_node_ns(self) -> None:
        assert effective_namespace(["sensing"], "lidar") == "/sensing/lidar"

    def test_stack_plus_absolute_node_ns(self) -> None:
        assert effective_namespace(["sensing", "lidar"], "/override") == "/override"


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestDataclassConstruction:
    def test_resolved_node_defaults(self) -> None:
        node = ResolvedNode()
        assert node.package == ""
        assert node.executable == ""
        assert node.kind == NodeKindTag.NODE
        assert node.parameters == {}
        assert node.remappings == []
        assert node.plugins == []

    def test_resolved_node_with_fields(self) -> None:
        node = ResolvedNode(
            package="my_pkg",
            executable="my_node",
            name="node1",
            namespace="/ns",
            parameters={"param1": "value1"},
            remappings=[("from", "to")],
            env={"ROS_DOMAIN_ID": "42"},
            kind=NodeKindTag.NODE,
            output="screen",
        )
        assert node.package == "my_pkg"
        assert node.name == "node1"
        assert node.parameters == {"param1": "value1"}
        assert node.output == "screen"

    def test_container_node(self) -> None:
        plugin = ComposablePlugin(
            package="my_pkg",
            plugin="my_pkg::MyComponent",
            name="component1",
            parameters={"p": "v"},
        )
        node = ResolvedNode(
            package="rclcpp_components",
            executable="component_container",
            kind=NodeKindTag.CONTAINER,
            plugins=[plugin],
        )
        assert node.kind == NodeKindTag.CONTAINER
        assert len(node.plugins) == 1
        assert node.plugins[0].plugin == "my_pkg::MyComponent"

    def test_param_file_reference(self) -> None:
        pf = ParamFileReference(display="config.yaml", abs="/path/config.yaml")
        assert pf.display == "config.yaml"

    def test_param_file_inlined(self) -> None:
        pf = ParamFileInlined(
            display="config.yaml",
            params=[("key1", "val1"), ("key2", "val2")],
        )
        assert len(pf.params) == 2

    def test_file_dependency(self) -> None:
        fd = FileDependency(
            package="my_pkg",
            share_path=Path("launch/foo.launch.xml"),
            kind=DependencyKind.LAUNCH,
        )
        assert fd.kind == DependencyKind.LAUNCH

    def test_include_arg_context_defaults(self) -> None:
        ctx = IncludeArgContext()
        assert ctx.explicit == {}
        assert ctx.with_cascade == {}
        assert ctx.namespace_stack == []

    def test_launch_include(self) -> None:
        inc = LaunchInclude(
            package="my_pkg",
            share_path=Path("launch/bar.launch.xml"),
            explicit_args={"arg1": "val1"},
            namespace_stack=["ns1"],
        )
        assert inc.package == "my_pkg"
        assert inc.namespace_stack == ["ns1"]

    def test_parsed_launch_file_defaults(self) -> None:
        plf = ParsedLaunchFile()
        assert plf.nodes == []
        assert plf.launch_includes == []
        assert plf.warnings == []

    def test_event_handler_kind_tag_name(self) -> None:
        assert EventHandlerKind.ON_PROCESS_START.tag_name == "on_process_start"
        assert EventHandlerKind.ON_PROCESS_EXIT.tag_name == "on_process_exit"
        assert EventHandlerKind.ON_STATE_TRANSITION.tag_name == "on_state_transition"
        assert EventHandlerKind.ON_SHUTDOWN.tag_name == "on_shutdown"

    def test_resolved_event_action(self) -> None:
        action = ResolvedEventAction(event="shutdown", target_node="node1")
        assert action.event == "shutdown"
        assert action.target_node == "node1"
        assert action.namespace is None


# ---------------------------------------------------------------------------
# SemanticNode + semantic_eq
# ---------------------------------------------------------------------------


class TestSemanticEq:
    def _make_node(self, **kwargs: object) -> ResolvedNode:
        return ResolvedNode(**kwargs)  # type: ignore[arg-type]

    def test_identical_nodes(self) -> None:
        nodes = [
            self._make_node(package="pkg", executable="exec", name="n1"),
            self._make_node(package="pkg", executable="exec2"),
        ]
        assert semantic_eq(nodes, list(nodes))

    def test_different_params(self) -> None:
        a = [self._make_node(package="p", executable="e", parameters={"k": "v1"})]
        b = [self._make_node(package="p", executable="e", parameters={"k": "v2"})]
        assert not semantic_eq(a, b)

    def test_ignores_source_and_include_chain(self) -> None:
        a = [
            self._make_node(
                package="p",
                executable="e",
                source=("pkg", Path("launch/a.launch.xml")),
                include_chain=[("root", Path("launch/root.launch.xml"))],
            )
        ]
        b = [
            self._make_node(
                package="p",
                executable="e",
                source=("pkg", Path("launch/b.launch.xml")),
                include_chain=[],
            )
        ]
        assert semantic_eq(a, b)

    def test_ignores_non_exec_kinds(self) -> None:
        a = [
            self._make_node(package="p", executable="e"),
            self._make_node(kind=NodeKindTag.INCLUDE_MARKER),
            self._make_node(kind=NodeKindTag.LOG, log_message="hello"),
            self._make_node(kind=NodeKindTag.SET_REMAP, remap_from="a", remap_to="b"),
        ]
        b = [self._make_node(package="p", executable="e")]
        assert semantic_eq(a, b)

    def test_different_order(self) -> None:
        a = [
            self._make_node(package="p1", executable="e1"),
            self._make_node(package="p2", executable="e2"),
        ]
        b = [
            self._make_node(package="p2", executable="e2"),
            self._make_node(package="p1", executable="e1"),
        ]
        assert not semantic_eq(a, b)

    def test_empty_lists(self) -> None:
        assert semantic_eq([], [])

    def test_semantic_node_from_resolved(self) -> None:
        node = self._make_node(
            package="p",
            executable="e",
            name="n",
            namespace="/ns",
            namespace_stack=["a", "b"],
            parameters={"z": "1", "a": "2"},
            remappings=[("from", "to")],
            env={"Z": "1", "A": "2"},
        )
        sn = SemanticNode.from_resolved(node)
        assert sn.package == "p"
        assert sn.namespace_stack == ("a", "b")
        # parameters and env are sorted by key
        assert sn.parameters == (("a", "2"), ("z", "1"))
        assert sn.env == (("A", "2"), ("Z", "1"))
