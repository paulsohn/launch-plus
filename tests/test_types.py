"""Tests for roscope.types — IR dataclasses and namespace helpers."""

from pathlib import Path

from roscope.types import (
    DependencyKind,
    FileDependency,
    IncludeArgContext,
    LaunchInclude,
    ParsedLaunchFile,
    effective_namespace,
    ros2_namespace_join,
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
        assert plf.launch_includes == []
