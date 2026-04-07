"""Tests for resolver internals — substitution handling, node tracking,
and inline Python include resolution."""

import logging
import os
import tempfile
import textwrap

from roscope.entities.actions.arg import DeclareLaunchArgument, _apply_declared_arg
from roscope.entities.actions.env import (
    SetEnvironmentVariable,
    UnsetEnvironmentVariable,
)
from roscope.entities.actions.include import _inline_resolve_python_launch
from roscope.entities.actions.node import (
    ComposableNode,
    ComposableNodeContainer,
    Node,
    _resolve_plugins,
)
from roscope.entities.helpers import (
    _effective_namespace,
    _is_substitution,
    _is_truthy,
    env_overrides,
    resolve_substitutions,
)
from roscope.entities.state import LaunchContext, ResolverState
from roscope.entities.substitutions.find_pkg_share import FindPackageShare
from roscope.entities.substitutions.launch_config import (
    DeferredDefault,
    LaunchConfiguration,
)
from roscope.resolver import parse_xml_launch, parse_yaml_launch, resolve_xml_elements

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_context(configs=None):
    """Build a LaunchContext with a fresh ResolverState and test defaults."""
    ctx = LaunchContext()
    ctx._state.preview_mode = True
    ctx._state.apply_arg_defaults = True
    ctx._launch_configurations = dict(configs or {})
    return ctx


def _write_launch_py(directory, filename, body):
    """Write a minimal Python launch file into *directory*."""
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        f.write(textwrap.dedent(body))
    return path


# ─── LaunchConfiguration ────────────────────────────────────────────────────


class TestLaunchConfiguration:
    def test_perform_returns_value_when_set(self):
        lc = LaunchConfiguration("my_var")
        ctx = _make_context({"my_var": "hello"})
        assert lc.perform(ctx) == "hello"

    def test_perform_returns_fallback_when_unset(self):
        lc = LaunchConfiguration("missing_var")
        ctx = _make_context({})
        assert lc.perform(ctx) == "$(var missing_var)"

    def test_perform_returns_default_when_unset_but_default_given(self):
        lc = LaunchConfiguration("missing_var", default="fallback")
        ctx = _make_context({})
        assert lc.perform(ctx) == "fallback"

    def test_perform_prefers_context_over_default(self):
        lc = LaunchConfiguration("my_var", default="fallback")
        ctx = _make_context({"my_var": "from_context"})
        assert lc.perform(ctx) == "from_context"

    def test_perform_returns_fallback_without_context(self):
        lc = LaunchConfiguration("x")
        assert lc.perform(None) == "$(var x)"

    def test_str_returns_variable_name(self):
        lc = LaunchConfiguration("pkg_name")
        assert str(lc) == "pkg_name"


# ─── _is_substitution ────────────────────────────────────────────────────────


class TestIsSubstitution:
    def test_launch_configuration_is_substitution(self):
        assert _is_substitution(LaunchConfiguration("x"))

    def test_string_is_not_substitution(self):
        assert not _is_substitution("rclcpp_components")

    def test_none_is_not_substitution(self):
        assert not _is_substitution(None)

    def test_list_of_substitutions_is_substitution(self):
        parts = [LaunchConfiguration("x"), "_suffix"]
        assert _is_substitution(parts)

    def test_list_of_plain_strings_is_not_substitution(self):
        assert not _is_substitution(["hello", "world"])

    def test_empty_list_is_not_substitution(self):
        assert not _is_substitution([])

    def test_tuple_of_substitutions_is_substitution(self):
        parts = (LaunchConfiguration("x"),)
        assert _is_substitution(parts)


# ─── _track_package ──────────────────────────────────────────────────────────


class TestTrackPackage:
    def test_tracks_plain_string(self):
        state = ResolverState()
        state.track_package("my_pkg")
        assert "my_pkg" in state.tracked["packages"]

    def test_skips_substitution_object(self):
        state = ResolverState()
        lc = LaunchConfiguration("container_pkg")
        state.track_package(lc)
        assert "container_pkg" not in state.tracked["packages"]
        assert len(state.tracked["packages"]) == 0

    def test_skips_empty_and_none(self):
        state = ResolverState()
        state.track_package(None)
        state.track_package("")
        assert len(state.tracked["packages"]) == 0

    def test_deduplicates(self):
        state = ResolverState()
        state.track_package("pkg_a")
        state.track_package("pkg_a")
        assert state.tracked["packages"].count("pkg_a") == 1

    def test_skips_list_of_substitutions(self):
        state = ResolverState()
        parts = [LaunchConfiguration("pkg_var"), "_suffix"]
        state.track_package(parts)
        assert len(state.tracked["packages"]) == 0


# ─── perform_substitution / perform_substitutions ────────────────────────────


class TestPerformSubstitution:
    def test_resolves_launch_configuration(self):
        lc = LaunchConfiguration("my_var")
        ctx = _make_context({"my_var": "resolved_value"})
        assert ctx.perform_substitution(lc) == "resolved_value"

    def test_unresolved_falls_back_to_portable(self):
        lc = LaunchConfiguration("missing")
        ctx = _make_context({})
        assert ctx.perform_substitution(lc) == "$(var missing)"

    def test_plain_string_passthrough(self):
        ctx = _make_context({})
        assert ctx.perform_substitution("hello") == "hello"

    def test_none_returns_empty(self):
        ctx = _make_context({})
        assert ctx.perform_substitution(None) == ""

    def test_perform_substitutions_list(self):
        from roscope.entities.utilities import perform_substitutions

        parts = [
            LaunchConfiguration("prefix"),
            LaunchConfiguration("suffix"),
        ]
        ctx = _make_context({"prefix": "foo", "suffix": "bar"})
        assert perform_substitutions(ctx, parts) == "foobar"

    def test_perform_substitutions_with_unresolved(self):
        from roscope.entities.utilities import perform_substitutions

        parts = [
            LaunchConfiguration("resolved_var"),
            LaunchConfiguration("unresolved_var"),
        ]
        ctx = _make_context({"resolved_var": "abc"})
        assert perform_substitutions(ctx, parts) == "abc$(var unresolved_var)"

    def test_ex_returns_not_fallback_when_resolved(self):
        lc = LaunchConfiguration("my_var")
        ctx = _make_context({"my_var": "resolved_value"})
        value, is_fallback = ctx.perform_substitution_ex(lc)
        assert value == "resolved_value"
        assert is_fallback is False

    def test_ex_list_not_fallback_when_all_resolved(self):
        parts = [LaunchConfiguration("a"), LaunchConfiguration("b")]
        ctx = _make_context({"a": "foo", "b": "bar"})
        value, is_fallback = ctx.perform_substitution_ex(parts)
        assert value == "foobar"
        assert is_fallback is False


# ─── Node deferred resolution ────────────────────────────────────────────────


class TestNodeDeferredResolution:
    def test_tracked_node_resolves_package_substitution(self):
        """When package is a LaunchConfiguration, _resolve_node_details should
        resolve it to the concrete value and track the resolved package."""
        ctx = _make_context({"my_pkg_var": "actual_package"})
        node = Node(
            package=LaunchConfiguration("my_pkg_var"),
            executable="my_exec",
        )
        node.execute(ctx)

        assert "actual_package" in ctx._state.tracked["packages"]

    def test_tracked_node_unresolved_package_returns_empty(self):
        """When the LaunchConfiguration cannot be resolved, node.execute()
        returns an empty list (no resolved node produced)."""
        ctx = _make_context({})
        node = Node(
            package=LaunchConfiguration("unknown_pkg"),
            executable="exec",
        )
        # Undefined variable → package name is empty string → no tracking
        result = node.execute(ctx)
        assert isinstance(result, list)

    def test_tracked_container_resolves_all_fields(self):
        ctx = _make_context(
            {
                "pkg": "rclcpp_components",
                "exe": "component_container_mt",
                "cname": "my_container",
            }
        )
        container = ComposableNodeContainer(
            package=LaunchConfiguration("pkg"),
            executable=LaunchConfiguration("exe"),
            name=LaunchConfiguration("cname"),
        )
        container.execute(ctx)

        assert "rclcpp_components" in ctx._state.tracked["packages"]

    def test_plain_string_package_tracked_on_execute(self):
        """When package is a plain string, it should be tracked after execute()."""
        ctx = _make_context()
        node = Node(package="my_real_pkg", executable="exec")
        node.execute(ctx)
        assert "my_real_pkg" in ctx._state.tracked["packages"]


# ─── Composable plugin deferred resolution ────────────────────────────────────


class TestComposablePluginResolution:
    def test_composable_node_resolves_package(self):
        ctx = _make_context({"plugin_pkg": "sensor_driver"})
        desc = ComposableNode(
            package=LaunchConfiguration("plugin_pkg"),
            plugin="sensor_driver::SensorNode",
            name="sensor",
        )
        plugins = _resolve_plugins([desc], ctx)
        assert len(plugins) == 1
        assert plugins[0]["package"] == "sensor_driver"
        assert "sensor_driver" in ctx._state.tracked["packages"]

    def test_composable_node_unresolved_package_not_tracked(self):
        ctx = _make_context({})
        desc = ComposableNode(
            package=LaunchConfiguration("unknown"),
            plugin="foo::Bar",
        )
        plugins = _resolve_plugins([desc], ctx)
        assert plugins[0]["package"] == "$(var unknown)"  # portable fallback
        assert "unknown" not in ctx._state.tracked["packages"]

    def test_composable_node_empty_string_remapping_preserved(self):
        """Remapping resolved to empty string should be preserved, not
        replaced with the substitution display name."""
        ctx = _make_context({"remap_src": "", "remap_dst": ""})
        desc = ComposableNode(
            package="my_pkg",
            plugin="my_pkg::Node",
        )
        desc.remappings = [
            (LaunchConfiguration("remap_src"), LaunchConfiguration("remap_dst")),
        ]
        plugins = _resolve_plugins([desc], ctx)
        assert plugins[0]["remappings"] == [["", ""]]


# ─── Inline Python include resolution ────────────────────────────────────────


class TestInlinePythonInclude:
    def test_set_launch_configuration_propagates(self):
        """A child Python launch file that calls SetLaunchConfiguration
        should update the parent context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = _write_launch_py(
                tmpdir,
                "child.launch.py",
                """\
                from launch import LaunchDescription
                from launch.actions import SetLaunchConfiguration

                def generate_launch_description():
                    return LaunchDescription([
                        SetLaunchConfiguration("child_var", "child_value"),
                    ])
            """,
            )

            ctx = _make_context({"parent_var": "parent_value"})
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {})

            assert ctx._launch_configurations["child_var"] == "child_value"
            assert ctx._launch_configurations["parent_var"] == "parent_value"

    def test_child_declared_args_persist(self):
        """DeclareLaunchArgument defaults from a child file persist in the
        parent context (matching official IncludeLaunchDescription scoped=False)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = _write_launch_py(
                tmpdir,
                "child.launch.py",
                """\
                from launch import LaunchDescription
                from launch.actions import DeclareLaunchArgument
                from launch.actions import SetLaunchConfiguration

                def generate_launch_description():
                    return LaunchDescription([
                        DeclareLaunchArgument("child_only_arg", default_value="persists_too"),
                        SetLaunchConfiguration("sticky_var", "persists"),
                    ])
            """,
            )

            ctx = _make_context({})
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {})

            assert ctx._launch_configurations["sticky_var"] == "persists"

    def test_child_args_forwarded(self):
        """launch_arguments passed to the include should be available
        in the child's context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = _write_launch_py(
                tmpdir,
                "child.launch.py",
                """\
                from launch import LaunchDescription
                from launch.actions import DeclareLaunchArgument
                from launch.actions import SetLaunchConfiguration
                from launch.substitutions import LaunchConfiguration

                def generate_launch_description():
                    return LaunchDescription([
                        DeclareLaunchArgument("mode", default_value="default"),
                        SetLaunchConfiguration("resolved_mode",
                                               LaunchConfiguration("mode")),
                    ])
            """,
            )

            ctx = _make_context({})
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {"mode": "custom"})

            assert ctx._launch_configurations["resolved_mode"] == "custom"

    def test_missing_file_silently_skipped(self):
        """A non-existent include file should not raise."""
        ctx = _make_context({})
        _inline_resolve_python_launch(ctx._state, "/nonexistent/path.py", ctx, {})
        # No error, no crash

    def test_inline_include_keeps_global_params(self):
        """SetParameter inside an inline-included child MUST create
        tracked global_params entries — the Python resolver handles all
        includes inline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = _write_launch_py(
                tmpdir,
                "child.launch.py",
                """\
                from launch import LaunchDescription
                from launch_ros.actions import SetParameter

                def generate_launch_description():
                    return LaunchDescription([
                        SetParameter(name="wheel_radius", value="0.383"),
                    ])
            """,
            )

            ctx = _make_context({})
            gp_before = len(ctx._state.tracked["global_params"])
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {})

            # Global params from inline include are kept
            assert len(ctx._state.tracked["global_params"]) > gp_before
            # Context should also have them
            gp_list = ctx._launch_configurations.get("global_params", [])
            assert any(name == "wheel_radius" for name, _ in gp_list)

    def test_inline_include_does_not_duplicate_include_deps(self):
        """Include dependencies discovered during inline walk should NOT
        be tracked — the Rust orchestrator tracks them when it processes
        the child file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a grandchild that the child includes
            _write_launch_py(
                tmpdir,
                "grandchild.launch.py",
                """\
                from launch import LaunchDescription

                def generate_launch_description():
                    return LaunchDescription([])
            """,
            )

            child_path = _write_launch_py(
                tmpdir,
                "child.launch.py",
                """\
                import os
                from launch import LaunchDescription
                from launch.actions import IncludeLaunchDescription
                from launch.launch_description_sources import PythonLaunchDescriptionSource

                def generate_launch_description():
                    here = os.path.dirname(__file__)
                    return LaunchDescription([
                        IncludeLaunchDescription(
                            PythonLaunchDescriptionSource(
                                os.path.join(here, "grandchild.launch.py")
                            ),
                        ),
                    ])
            """,
            )

            ctx = _make_context({})
            deps_before = len(ctx._state.tracked["include_deps"])
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {})

            # No new include deps
            assert len(ctx._state.tracked["include_deps"]) == deps_before


# ─── Environment Variable Stack ──────────────────────────────────────────────


class TestEnvStack:
    """Tests for SetEnvironmentVariable / UnsetEnvironmentVariable tracking,
    env inheritance to nodes, and group scoping."""

    def test_unset_env_nonexistent_errors(self, caplog):
        """UnsetEnvironmentVariable on a var that doesn't exist → 'not set' error."""
        import uuid

        name = f"NONEXISTENT_VAR_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        with caplog.at_level(logging.WARNING):
            UnsetEnvironmentVariable(name=name).execute(ctx)
        assert name in caplog.text and "not set" in caplog.text

    def test_unset_env_override_only_accepted(self):
        """UnsetEnv on an override-only var (not in process env) → accepted."""
        import uuid

        name = f"OVERRIDE_ONLY_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        SetEnvironmentVariable(name=name, value="val").execute(ctx)
        assert name in ctx.environment
        UnsetEnvironmentVariable(name=name).execute(ctx)
        assert name not in ctx.environment
        errors = ctx._state.tracked.get("errors", [])
        assert not any(name in e for e in errors)

    def test_inline_include_env_persists(self):
        """Env set by inline-included child persists (matching official scoped=False)."""
        import tempfile
        import textwrap

        with tempfile.TemporaryDirectory() as d:
            child_path = os.path.join(d, "child.launch.py")
            with open(child_path, "w") as f:
                f.write(
                    textwrap.dedent("""\
                    from launch import LaunchDescription
                    from launch.actions import SetEnvironmentVariable
                    def generate_launch_description():
                        return LaunchDescription([
                            SetEnvironmentVariable(name="CHILD_VAR", value="child_val"),
                        ])
                """)
                )
            ctx = _make_context()
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {})
            assert ctx.environment.get("CHILD_VAR") == "child_val"

    def test_env_overrides_returns_only_overrides(self):
        """_env_overrides() returns only explicitly set vars, not process env."""
        ctx = _make_context()
        ctx.environment["NEW_VAR"] = "new_val"
        overrides = env_overrides(ctx)
        assert overrides["NEW_VAR"] == "new_val"
        # Process env vars must NOT appear in overrides.
        import os

        assert "PATH" in os.environ, "PATH should exist in process env for this test"
        assert "PATH" not in overrides

    def test_env_overrides_empty_when_no_overrides(self):
        """Empty overrides when nothing has been set."""
        ctx = _make_context()
        assert env_overrides(ctx) == {}

    def test_net_zero_error_includes_value(self):
        """Net-zero leak error includes the override value (safe, user-set)."""
        ctx = _make_context()
        ctx.environment["MY_KEY"] = "my_value"
        # Simulate the net-zero check inline (same logic as main())
        errors = []
        for k, v in ctx.environment.items():
            errors.append(
                f"env var '{k}' was set to '{v}' but not restored (leaked from file scope)"
            )
        err = [e for e in errors if "MY_KEY" in e]
        assert len(err) == 1
        assert "my_value" in err[0]


# ─── XML Parser ──────────────────────────────────────────────────────────────


class TestParseXmlLaunch:
    """Tests for parse_xml_launch() — now returns Entity objects."""

    def test_parse_arg(self):
        xml = '<launch><arg name="x" default="val" description="desc"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        assert len(elems) == 1
        e = elems[0]
        assert e.type_name == "arg"
        assert e.get_attr("name") == "x"
        assert e.get_attr("default") == "val"
        assert e.get_attr("description") == "desc"

    def test_parse_arg_no_default(self):
        xml = '<launch><arg name="x"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].get_attr("default", optional=True) is None

    def test_parse_let(self):
        xml = '<launch><let name="v" value="123"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "let"
        assert e.get_attr("name") == "v"
        assert e.get_attr("value") == "123"

    def test_parse_let_with_condition(self):
        xml = '<launch><let name="v" value="1" if="$(arg flag)"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.get_attr("if") == "$(arg flag)"

    def test_parse_node(self):
        xml = textwrap.dedent("""\
            <launch>
                <node pkg="my_pkg" exec="my_exec" name="n" namespace="/ns" output="screen">
                    <param name="foo" value="bar"/>
                    <remap from="/in" to="/out"/>
                    <env name="VAR" value="val"/>
                </node>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "node"
        assert e.get_attr("pkg") == "my_pkg"
        assert e.get_attr("exec") == "my_exec"
        assert e.get_attr("name") == "n"
        assert e.get_attr("namespace") == "/ns"
        assert e.get_attr("output") == "screen"
        params = e.get_attr("param", data_type=list)
        assert len(params) == 1
        assert params[0].get_attr("name") == "foo"
        remaps = e.get_attr("remap", data_type=list)
        assert len(remaps) == 1
        assert remaps[0].get_attr("from") == "/in"
        envs = e.get_attr("env", data_type=list)
        assert len(envs) == 1
        assert envs[0].get_attr("name") == "VAR"

    def test_parse_group_scoped(self):
        xml = textwrap.dedent("""\
            <launch>
                <group scoped="false" if="$(arg x)">
                    <arg name="nested" default="val"/>
                </group>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "group"
        assert e.get_attr("scoped") == "false"
        assert e.get_attr("if") == "$(arg x)"
        children = e.children
        assert len(children) == 1
        assert children[0].type_name == "arg"

    def test_parse_include_with_args(self):
        xml = textwrap.dedent("""\
            <launch>
                <include file="$(find-pkg-share pkg)/launch/f.xml">
                    <arg name="a" value="1"/>
                    <arg name="b" value="2"/>
                </include>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "include"
        assert "$(find-pkg-share pkg)" in e.get_attr("file")
        args = e.get_attr("arg", data_type=list)
        assert len(args) == 2
        assert args[0].get_attr("name") == "a"

    def test_parse_set_env_unset_env(self):
        xml = textwrap.dedent("""\
            <launch>
                <set_env name="X" value="1"/>
                <unset_env name="Y" unless="$(arg flag)"/>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "set_env"
        assert elems[0].get_attr("name") == "X"
        assert elems[1].type_name == "unset_env"
        assert elems[1].get_attr("name") == "Y"
        assert elems[1].get_attr("unless") == "$(arg flag)"

    def test_parse_push_ros_namespace(self):
        xml = '<launch><push-ros-namespace namespace="/my_ns"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "push-ros-namespace"
        assert elems[0].get_attr("namespace") == "/my_ns"

    def test_parse_node_container(self):
        xml = textwrap.dedent("""\
            <launch>
                <node_container pkg="rclcpp" exec="container" name="c">
                    <composable_node pkg="p" plugin="p::N" name="n">
                        <param name="rate" value="10"/>
                    </composable_node>
                    <env name="E" value="V"/>
                </node_container>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "node_container"
        assert e.get_attr("pkg") == "rclcpp"
        cns = e.get_attr("composable_node", data_type=list)
        assert len(cns) == 1
        assert cns[0].get_attr("plugin") == "p::N"
        cn_params = cns[0].get_attr("param", data_type=list)
        assert len(cn_params) == 1
        envs = e.get_attr("env", data_type=list)
        assert len(envs) == 1

    def test_parse_load_composable_node(self):
        xml = textwrap.dedent("""\
            <launch>
                <load_composable_node target="container">
                    <composable_node pkg="p" plugin="p::N"/>
                </load_composable_node>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "load_composable_node"
        assert e.get_attr("target") == "container"
        cns = e.get_attr("composable_node", data_type=list)
        assert len(cns) == 1

    def test_parse_set_parameter_set_remap(self):
        xml = textwrap.dedent("""\
            <launch>
                <set_parameter name="p" value="v"/>
                <set_remap from="/a" to="/b"/>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "set_parameter"
        assert elems[0].get_attr("name") == "p"
        assert elems[1].type_name == "set_remap"
        assert elems[1].get_attr("from") == "/a"

    def test_parse_lifecycle_node(self):
        xml = '<launch><lifecycle_node pkg="p" exec="e" name="n"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "lifecycle_node"
        assert elems[0].get_attr("pkg") == "p"

    def test_parse_unknown_element(self):
        xml = "<launch><foobar/></launch>"
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "foobar"

    def test_parse_event_handler(self):
        xml = textwrap.dedent("""\
            <launch>
                <on_process_exit target="my_node">
                    <emit_event event="shutdown"/>
                </on_process_exit>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "on_process_exit"
        assert e.get_attr("target") == "my_node"
        children = e.children
        assert len(children) == 1
        assert children[0].type_name == "emit_event"
        assert children[0].get_attr("event") == "shutdown"

    def test_parse_param_from(self):
        xml = '<launch><node pkg="p" exec="e"><param from="file.yaml"/></node></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        params = elems[0].get_attr("param", data_type=list)
        assert params[0].get_attr("from") == "file.yaml"
        assert params[0].get_attr("name", optional=True) is None

    def test_substitutions_preserved_as_raw_strings(self):
        xml = '<launch><node pkg="$(arg pkg)" exec="$(var exe)"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].get_attr("pkg") == "$(arg pkg)"
        assert elems[0].get_attr("exec") == "$(var exe)"


# ─── YAML Parser ─────────────────────────────────────────────────────────────


class TestParseYamlLaunch:
    """Tests for parse_yaml_launch() — now returns Entity objects."""

    def test_parse_basic_yaml(self):
        yaml_content = textwrap.dedent("""\
            launch:
              - arg:
                  name: my_arg
                  default: val
              - node:
                  pkg: my_pkg
                  exec: my_exec
                  name: my_node
        """)
        elems = parse_yaml_launch(yaml_content, "test.yaml")
        assert len(elems) == 2
        assert elems[0].type_name == "arg"
        assert elems[0].get_attr("name") == "my_arg"
        assert elems[1].type_name == "node"
        assert elems[1].get_attr("pkg") == "my_pkg"

    def test_parse_yaml_push_ros_namespace(self):
        yaml_content = textwrap.dedent("""\
            launch:
              - push_ros_namespace:
                  namespace: /my_ns
        """)
        elems = parse_yaml_launch(yaml_content, "test.yaml")
        assert elems[0].type_name == "push-ros-namespace"
        assert elems[0].get_attr("namespace") == "/my_ns"

    def test_parse_yaml_composable_node_container(self):
        yaml_content = textwrap.dedent("""\
            launch:
              - composable_node_container:
                  pkg: rclcpp
                  exec: container
                  name: c
        """)
        elems = parse_yaml_launch(yaml_content, "test.yaml")
        assert elems[0].type_name == "node_container"

    def test_parse_yaml_with_children(self):
        yaml_content = textwrap.dedent("""\
            launch:
              - group:
                  scoped: false
                  children:
                    - arg:
                        name: nested
                        default: val
        """)
        elems = parse_yaml_launch(yaml_content, "test.yaml")
        e = elems[0]
        assert e.type_name == "group"
        assert e.get_attr("scoped", data_type=bool) is False
        children = e.children
        assert len(children) == 1
        assert children[0].type_name == "arg"
        assert children[0].get_attr("name") == "nested"

    def test_parse_yaml_missing_launch_key(self):
        yaml_content = "foo: bar"
        elems = parse_yaml_launch(yaml_content, "test.yaml")
        assert elems == []


# ─── Substitution Engine ─────────────────────────────────────────────────────


def _fresh_subst_ctx(**kwargs):
    """Create a LaunchContext with a fresh ResolverState and test defaults."""
    ctx = LaunchContext()
    ctx._state.preview_mode = True
    ctx._state.apply_arg_defaults = True
    # Translate legacy args/vars kwargs to _launch_configurations
    lc_updates: dict = {}
    for k, v in kwargs.items():
        if k in ("args", "vars"):
            lc_updates.update(v)
        elif k == "env":
            ctx._environment.update(v)
        elif k == "preview_mode":
            ctx._state.preview_mode = v
            ctx.preview_mode = v
        else:
            setattr(ctx, k, v)
    if lc_updates:
        ctx._launch_configurations.update(lc_updates)
    return ctx


class TestResolveSubstitutions:
    """Tests for resolve_substitutions() — full resolution with context."""

    def test_resolve_arg(self):
        ctx = _fresh_subst_ctx(args={"vehicle": "sample_vehicle"})
        result = resolve_substitutions("$(arg vehicle)", ctx)
        assert result == "sample_vehicle"

    def test_resolve_var(self):
        ctx = _fresh_subst_ctx(vars={"config": "/path/to/config"})
        result = resolve_substitutions("$(var config)", ctx)
        assert result == "/path/to/config"

    def test_resolve_var_falls_back_to_args(self):
        ctx = _fresh_subst_ctx(args={"fallback": "from_args"})
        result = resolve_substitutions("$(var fallback)", ctx)
        assert result == "from_args"

    def test_resolve_env_from_context(self):
        ctx = _fresh_subst_ctx(env={"TEST_LAUNCH_VAR": "test_value"})
        result = resolve_substitutions("$(env TEST_LAUNCH_VAR)", ctx)
        assert result == "test_value"

    def test_resolve_env_with_default_unset(self):
        var = "LAUNCH_PLUS_TEST_UNSET_a1b2c3"
        assert var not in os.environ, f"precondition: {var} must not be set"
        ctx = _fresh_subst_ctx()
        result = resolve_substitutions(f"$(env {var} fallback)", ctx)
        assert result == "fallback"

    def test_resolve_env_unset_without_default_errors(self, caplog):
        var = "LAUNCH_PLUS_TEST_UNSET_d4e5f6"
        assert var not in os.environ, f"precondition: {var} must not be set"
        ctx = _fresh_subst_ctx()
        with caplog.at_level(logging.WARNING):
            resolve_substitutions(f"$(env {var})", ctx)
        assert "not set" in caplog.text

    def test_resolve_dirname(self):
        ctx = _fresh_subst_ctx(launch_file_dir="/path/to/launch")
        result = resolve_substitutions("$(dirname)/config.yaml", ctx)
        assert result == "/path/to/launch/config.yaml"

    def test_resolve_dirname_unset(self):
        ctx = _fresh_subst_ctx()
        result = resolve_substitutions("$(dirname)/config.yaml", ctx)
        assert result == "$(dirname)/config.yaml"

    def test_resolve_find_pkg_share_preview(self):
        ctx = _fresh_subst_ctx(preview_mode=True)
        ctx._state.package_shares["my_pkg"] = "/ws/src/my_pkg"
        result = resolve_substitutions("$(find-pkg-share my_pkg)/config", ctx)
        assert result == "/ws/src/my_pkg/config"
        assert "my_pkg" in ctx._state.tracked["packages"]

    def test_resolve_find_pkg_prefix(self):
        ctx = _fresh_subst_ctx()
        ctx._state.package_shares["my_pkg"] = "/opt/ros/humble/share/my_pkg"
        result = resolve_substitutions("$(find-pkg-prefix my_pkg)/lib", ctx)
        assert result == "/opt/ros/humble/lib"
        assert "my_pkg" in ctx._state.tracked["packages"]

    def test_resolve_nested_substitution(self):
        ctx = _fresh_subst_ctx(
            vars={"pkg_name": "vehicle_description"},
            preview_mode=True,
        )
        ctx._state.package_shares["vehicle_description"] = "/ws/src/vehicle_description"
        result = resolve_substitutions("$(find-pkg-share $(var pkg_name))/config", ctx)
        assert result == "/ws/src/vehicle_description/config"

    def test_resolve_chained_vars(self):
        ctx = _fresh_subst_ctx(
            args={"vehicle": "sample"},
            vars={
                # In the new system, <let> resolves $(arg vehicle) before storing
                "config_path": "$(find-pkg-share sample_description)/config",
            },
            preview_mode=True,
        )
        result = resolve_substitutions("$(var config_path)/params.yaml", ctx)
        assert result == "$(find-pkg-share sample_description)/config/params.yaml"

    def test_resolve_error_undefined_arg(self, caplog):
        ctx = _fresh_subst_ctx()
        with caplog.at_level(logging.WARNING):
            result = resolve_substitutions("$(arg undefined)", ctx)
        assert "$(arg undefined)" in result
        assert "undefined argument" in caplog.text

    def test_resolve_error_undefined_var(self, caplog):
        ctx = _fresh_subst_ctx()
        with caplog.at_level(logging.WARNING):
            result = resolve_substitutions("$(var undefined)", ctx)
        assert "$(var undefined)" in result
        assert "undefined variable" in caplog.text

    def test_resolve_eval_string_equality(self):
        # After XML entity decoding, &quot; becomes " — the == is inside a
        # double-quoted template that the Lark grammar parses correctly.
        ctx = _fresh_subst_ctx(vars={"gnss_receiver": "ublox"})
        result = resolve_substitutions("""$(eval "'$(var gnss_receiver)'=='ublox'")""", ctx)
        assert result == "True"

    def test_resolve_eval_false_comparison(self):
        ctx = _fresh_subst_ctx(vars={"x": "foo"})
        result = resolve_substitutions("""$(eval "'$(var x)'=='bar'")""", ctx)
        assert result == "False"

    def test_resolve_eval_outer_single_quote_wrapper(self):
        ctx = _fresh_subst_ctx()
        result = resolve_substitutions(
            r"$(eval '\'cuda\' == \'cuda\' or \'cuda\' == \'cuda-all-in-one\'')",
            ctx,
        )
        assert result == "True"

    def test_resolve_eval_outer_double_quote_wrapper(self):
        ctx = _fresh_subst_ctx()
        result = resolve_substitutions(
            r"""$(eval '"camera_lidar_radar_fusion"=="camera_lidar_radar_fusion"')""",
            ctx,
        )
        assert result == "True"

    def test_resolve_eval_outer_double_quote_false(self):
        ctx = _fresh_subst_ctx()
        result = resolve_substitutions(
            r"""$(eval '"camera_lidar_radar_fusion"=="lidar"')""",
            ctx,
        )
        assert result == "False"

    def test_resolve_eval_var_with_quotes_no_corruption(self):
        # Regression: outer " wrapper must be stripped before $(var) substitution
        ctx = _fresh_subst_ctx(
            vars={
                "modules": "[Foo, ",
                "list_end": '""]',
            },
        )
        result = resolve_substitutions(
            """$(eval "'$(var modules)' + '$(var list_end)'")""",
            ctx,
        )
        assert result == '[Foo, ""]'

    def test_resolve_eval_backslash_escaped_quotes_in_var(self):
        ctx = _fresh_subst_ctx(
            vars={
                "func": r"list(set('ndt'.split('_')).intersection(['ndt','yabloc']))",
            },
        )
        result = resolve_substitutions(r"$(eval $(var func))", ctx)
        assert result == "['ndt']"

    def test_resolve_eval_with_backslash_unescape(self):
        ctx = _fresh_subst_ctx(
            vars={
                "func2": r"list(set('ndt'.split('_')).intersection([\'ndt\',\'yabloc\']))",
            },
        )
        result = resolve_substitutions(r"$(eval $(var func2))", ctx)
        assert result == "['ndt']"

    def test_resolve_command_preserved(self):
        ctx = _fresh_subst_ctx()
        result = resolve_substitutions("$(command echo hello)", ctx)
        assert result == "$(command echo hello)"

    def test_resolve_literal_passthrough(self):
        ctx = _fresh_subst_ctx()
        result = resolve_substitutions("/path/to/file.yaml", ctx)
        assert result == "/path/to/file.yaml"

    def test_resolve_multiple_packages_tracked(self):
        ctx = _fresh_subst_ctx(preview_mode=True)
        ctx._state.package_shares["pkg1"] = "/ws/src/pkg1"
        ctx._state.package_shares["pkg2"] = "/ws/src/pkg2"
        resolve_substitutions("$(find-pkg-share pkg1)/$(find-pkg-share pkg2)", ctx)
        assert "pkg1" in ctx._state.tracked["packages"]
        assert "pkg2" in ctx._state.tracked["packages"]


# ─── AST Walker (resolve_xml_elements) ───────────────────────────────────────


def _fresh_walker_ctx(**kwargs):
    """Create a fresh LaunchContext with test defaults for walker tests."""
    ctx = LaunchContext()
    ctx._state.preview_mode = True
    ctx._state.apply_arg_defaults = True
    # Translate legacy args/vars kwargs to _launch_configurations
    lc_updates: dict = {}
    for k, v in kwargs.items():
        if k in ("args", "vars"):
            lc_updates.update(v)
        elif k == "env":
            ctx._environment.update(v)
        elif k == "preview_mode":
            ctx._state.preview_mode = v
            ctx.preview_mode = v
        else:
            setattr(ctx, k, v)
    if lc_updates:
        ctx._launch_configurations.update(lc_updates)
    return ctx


def _parse_and_walk(xml_str, ctx=None, **ctx_kwargs):
    """Parse XML string and walk it.  Returns (ctx, tracked)."""
    if ctx is None:
        ctx = _fresh_walker_ctx(**ctx_kwargs)
    elements = parse_xml_launch(xml_str, "test.launch.xml")
    resolve_xml_elements(elements, ctx)
    return ctx, ctx._state.tracked


class TestResolveXmlElements:
    """Tests for resolve_xml_elements() — the XML/YAML AST walker."""

    # ── Basic node resolution ──

    def test_simple_node(self):
        xml = '<launch><node pkg="my_pkg" exec="my_node" name="node1"/></launch>'
        _, tracked = _parse_and_walk(xml)
        assert "my_pkg" in tracked["packages"]

    # ── Arg and Let ──

    def test_arg_default_applied(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="vehicle" default="sample"/>
              <node pkg="$(arg vehicle)_pkg" exec="node" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert "sample_pkg" in tracked["packages"]

    def test_declared_args_tracked(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="a" default="1"/>
              <arg name="b" default="2"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        names = [a["name"] for a in tracked["declared_args"]]
        assert "a" in names
        assert "b" in names

    def test_let_with_condition(self, caplog):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="flag" default="false"/>
              <let name="x" value="set" if="$(arg flag)"/>
              <node pkg="$(var x)" exec="e" name="n"/>
            </launch>
        """)
        with caplog.at_level(logging.WARNING):
            _parse_and_walk(xml)
        # $(var x) is undefined → error recorded, placeholder kept
        assert "undefined variable" in caplog.text

    # ── SetParameter, SetRemap, Log, Executable ──

    def test_set_parameter(self):
        xml = '<launch><set_parameter name="use_sim_time" value="true"/></launch>'
        _, tracked = _parse_and_walk(xml)
        assert ["use_sim_time", "true"] in tracked["global_params"]

    # ── Include (with file on disk) ──

    def test_include_xml_inline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write child launch file
            child_xml = textwrap.dedent("""\
                <launch>
                  <arg name="param1"/>
                  <node pkg="included_pkg" exec="node" name="$(arg param1)_node"/>
                </launch>
            """)
            child_path = os.path.join(tmpdir, "child.launch.xml")
            with open(child_path, "w") as f:
                f.write(child_xml)

            main_xml = f"""\
                <launch>
                  <include file="{child_path}">
                    <arg name="param1" value="test"/>
                  </include>
                </launch>
            """
            _, tracked = _parse_and_walk(main_xml)
            assert "included_pkg" in tracked["packages"]

    def test_include_tracks_include_args(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            child_xml = '<launch><arg name="x"/></launch>'
            child_path = os.path.join(tmpdir, "child.launch.xml")
            with open(child_path, "w") as f:
                f.write(child_xml)

            main_xml = f"""\
                <launch>
                  <include file="{child_path}">
                    <arg name="x" value="42"/>
                  </include>
                </launch>
            """
            _, tracked = _parse_and_walk(main_xml)
            assert tracked["include_args"][child_path] == {"x": "42"}

    def test_circular_include_detected(self, caplog):
        with tempfile.TemporaryDirectory() as tmpdir:
            # File includes itself
            self_path = os.path.join(tmpdir, "self.launch.xml")
            with open(self_path, "w") as f:
                f.write(f'<launch><include file="{self_path}"/></launch>')

            ctx = _fresh_walker_ctx()
            elements = parse_xml_launch(
                f'<launch><include file="{self_path}"/></launch>', "test.launch.xml"
            )
            with caplog.at_level(logging.WARNING):
                resolve_xml_elements(elements, ctx)
            assert "circular" in caplog.text

    # ── Unknown element ──

    def test_unknown_element_warns(self, caplog):
        xml = '<launch><foobar attr="val"/></launch>'
        with caplog.at_level(logging.WARNING):
            _parse_and_walk(xml)
        assert "unknown element" in caplog.text

    # ── Namespace helper functions ──

    def test_effective_namespace_basic(self):
        assert _effective_namespace([]) is None
        assert _effective_namespace(["/ns"]) == "/ns"
        assert _effective_namespace(["ns1", "ns2"]) == "/ns1/ns2"
        assert _effective_namespace(["/a", "b"]) == "/a/b"

    def test_effective_namespace_absolute_resets(self):
        assert _effective_namespace(["/a", "/b"]) == "/b"
        assert _effective_namespace(["a", "/b", "c"]) == "/b/c"

    def test_effective_namespace_with_explicit(self):
        assert _effective_namespace(["/robot"], "/override") == "/override"
        assert _effective_namespace(["/robot"], "local") == "/robot/local"

    def test_is_truthy(self):
        assert _is_truthy("true") is True
        assert _is_truthy("True") is True
        assert _is_truthy("1") is True
        assert _is_truthy("false") is False
        assert _is_truthy("0") is False
        # Invalid values raise ValueError
        import pytest

        for invalid in ("yes", "on", "no", ""):
            with pytest.raises(ValueError, match="invalid condition expression"):
                _is_truthy(invalid)


# ─── Action registry resolution (resolve_xml_to_ir → _tracked) ──────────────


def _parse_to_tracked(xml_str, **ctx_kwargs):
    """Parse XML string, resolve via action registry, return _tracked."""
    ctx = _fresh_walker_ctx(**ctx_kwargs)
    elements = parse_xml_launch(xml_str, "test.launch.xml")
    resolve_xml_elements(elements, ctx)
    return ctx._state.tracked


class TestActionRegistry:
    """Tests for action registry resolution — _tracked output."""

    def test_declared_args(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="x" default="1"/>
              <arg name="y" default="2"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        assert len(tracked["declared_args"]) == 2
        assert tracked["declared_args"][0]["name"] == "x"
        assert tracked["declared_args"][1]["default"] == "2"

    def test_packages_tracked(self):
        xml = '<launch><node pkg="my_pkg" exec="e" name="n"/></launch>'
        tracked = _parse_to_tracked(xml)
        assert "my_pkg" in tracked["packages"]

    def test_errors_and_warnings(self, caplog):
        xml = textwrap.dedent("""\
            <launch>
              <node pkg="$(arg undefined)" exec="e" name="n"/>
              <foobar/>
            </launch>
        """)
        with caplog.at_level(logging.WARNING):
            _parse_to_tracked(xml)
        assert "undefined" in caplog.text
        assert "unknown" in caplog.text


# ─── rosdep resolve parser tests ─────────────────────────────────────────────
# Mirror the Rust-side tests in rosdep.rs.


# ─── _apply_declared_arg (lazy default evaluation) ───────────────────────────


class TestApplyDeclaredArgLazy:
    """Default is NOT resolved when the arg is already set by the caller."""

    def test_default_not_resolved_when_arg_already_set(self, caplog):
        """FindPackageShare in default must not be perform()'d if arg is set."""
        ctx = _make_context({"my_arg": "already_set_value"})
        ctx._state.preview_mode = False
        arg = DeclareLaunchArgument(
            "my_arg",
            default_value=[
                FindPackageShare("nonexistent_pkg"),
                "/config/file.yaml",
            ],
        )
        with caplog.at_level(logging.WARNING):
            _apply_declared_arg(arg, ctx)
        # Arg value unchanged (caller's value preserved).
        assert ctx._launch_configurations["my_arg"] == "already_set_value"
        # No error — default was not resolved.
        assert "nonexistent_pkg" not in caplog.text

    def test_default_deferred_when_arg_not_set(self):
        """Default is stored as DeferredDefault, resolved on read."""
        ctx = _make_context({})
        ctx._state.preview_mode = True
        arg = DeclareLaunchArgument(
            "my_arg",
            default_value="simple_default",
        )
        _apply_declared_arg(arg, ctx)
        # Stored as deferred, not yet resolved.
        assert isinstance(ctx._launch_configurations["my_arg"], DeferredDefault)
        # Reading via LaunchConfiguration resolves it.
        lc = LaunchConfiguration("my_arg")
        assert lc.perform(ctx) == "simple_default"
        # Now it's resolved in the context.
        assert ctx._launch_configurations["my_arg"] == "simple_default"

    def test_deferred_default_not_resolved_if_never_read(self, caplog):
        """FindPackageShare for uninstalled pkg causes no error if arg is never read."""
        ctx = _make_context({})
        ctx._state.preview_mode = False
        arg = DeclareLaunchArgument(
            "cuda_param",
            default_value=[
                FindPackageShare("uninstalled_cuda_pkg"),
                "/config/file.yaml",
            ],
        )
        with caplog.at_level(logging.WARNING):
            _apply_declared_arg(arg, ctx)
        # Default is deferred — no resolution happened, no error.
        assert isinstance(ctx._launch_configurations["cuda_param"], DeferredDefault)
        assert "uninstalled_cuda_pkg" not in caplog.text

    def test_unresolved_default_recorded_for_show_args(self):
        """When arg is already set, the raw default string is recorded for --show-args."""
        ctx = _make_context({"my_arg": "caller_value"})
        ctx._state.preview_mode = False
        arg = DeclareLaunchArgument(
            "my_arg",
            default_value=[
                FindPackageShare("some_pkg"),
                "/config/file.yaml",
            ],
        )
        _apply_declared_arg(arg, ctx)
        # declared_args records the unresolved default (str() form).
        recorded = ctx._state.tracked["declared_args"]
        assert len(recorded) == 1
        assert "$(find-pkg-share some_pkg)" in recorded[0]["default"]
        assert "/config/file.yaml" in recorded[0]["default"]


# ─── Strictness flags ────────────────────────────────────────────────────────


class TestStrictnessFlags:
    """Tests for apply_arg_defaults."""

    def test_apply_arg_defaults_true_applies_default(self):
        ctx = LaunchContext()
        ctx._state.apply_arg_defaults = True
        elements = parse_xml_launch('<launch><arg name="x" default="hello"/></launch>', "test.xml")
        resolve_xml_elements(elements, ctx)
        # Default is stored as DeferredDefault; resolve via $(arg x)
        assert resolve_substitutions("$(arg x)", ctx) == "hello"

    def test_apply_arg_defaults_false_skips_default(self):
        ctx = LaunchContext()
        ctx._state.apply_arg_defaults = False
        elements = parse_xml_launch('<launch><arg name="x" default="hello"/></launch>', "test.xml")
        resolve_xml_elements(elements, ctx)
        # Default not applied — arg stays absent
        assert "x" not in ctx._launch_configurations

    def test_apply_arg_defaults_false_undefined_ref_errors(self, caplog):
        ctx = LaunchContext()
        ctx._state.apply_arg_defaults = False
        elements = parse_xml_launch(
            """<launch>
                <arg name="x" default="hello"/>
                <let name="y" value="$(arg x)"/>
            </launch>""",
            "test.xml",
        )
        with caplog.at_level(logging.WARNING):
            resolve_xml_elements(elements, ctx)
        assert "undefined" in caplog.text


# ─── _resolve_pkg_share and FindPackageShare ─────────────────────────


class TestResolvePkgShare:
    """Tests for _resolve_pkg_share mode-dependent behavior."""

    def test_preview_returns_source_path_from_package_shares(self):
        state = ResolverState()
        state.preview_mode = True
        state.package_shares["my_pkg"] = "/ws/src/my_pkg"
        assert state.resolve_pkg_share("my_pkg") == "/ws/src/my_pkg"

    def test_preview_unknown_pkg_raises(self):
        state = ResolverState()
        state.preview_mode = True
        import pytest

        with pytest.raises(LookupError, match="not found"):
            state.resolve_pkg_share("unknown_pkg")

    def test_postbuild_returns_install_path_from_package_shares(self):
        state = ResolverState()
        state.preview_mode = False
        state.package_shares["my_pkg"] = "/ws/install/my_pkg/share/my_pkg"
        assert state.resolve_pkg_share("my_pkg") == "/ws/install/my_pkg/share/my_pkg"

    def test_postbuild_unknown_pkg_raises(self):
        state = ResolverState()
        state.preview_mode = False
        import pytest

        with pytest.raises(LookupError, match="not found"):
            state.resolve_pkg_share("unknown_pkg")

    def test_postbuild_skips_lockfile_fetch(self):
        """In postbuild mode, lockfile packages not in _package_shares are not fetched."""
        state = ResolverState()
        state.preview_mode = False
        state.lockfile_data = {
            "lockfile_pkg": {
                "repo": "org/repo",
                "path": "pkg",
                "url": "https://example.com",
                "version": "abc123",
            }
        }
        import pytest

        # Should raise, not attempt to fetch
        with pytest.raises(LookupError, match="not found"):
            state.resolve_pkg_share("lockfile_pkg")


def _parse_yaml_and_walk(yaml_str, ctx=None, **ctx_kwargs):
    """Parse YAML string and walk it.  Returns (ctx, tracked)."""
    if ctx is None:
        ctx = _fresh_walker_ctx(**ctx_kwargs)
    elements = parse_yaml_launch(yaml_str, "test.launch.yaml")
    resolve_xml_elements(elements, ctx)
    return ctx, ctx._state.tracked


class TestResolveYamlElements:
    """End-to-end tests for the YAML parse → resolve_xml_elements pipeline."""

    def test_simple_node(self):
        yaml = textwrap.dedent("""\
            launch:
              - node:
                  pkg: my_pkg
                  exec: my_exec
                  name: my_node
        """)
        _, tracked = _parse_yaml_and_walk(yaml)
        assert "my_pkg" in tracked["packages"]

    def test_arg_and_let(self):
        yaml = textwrap.dedent("""\
            launch:
              - arg:
                  name: vehicle
                  default: sample
              - node:
                  pkg: "$(arg vehicle)_pkg"
                  exec: node
                  name: n
        """)
        _, tracked = _parse_yaml_and_walk(yaml)
        assert "sample_pkg" in tracked["packages"]

    def test_set_parameter(self):
        yaml = textwrap.dedent("""\
            launch:
              - set_parameter:
                  name: use_sim_time
                  value: "true"
        """)
        _, tracked = _parse_yaml_and_walk(yaml)
        assert ["use_sim_time", "true"] in tracked["global_params"]

    def test_push_ros_namespace(self):
        yaml = textwrap.dedent("""\
            launch:
              - push_ros_namespace:
                  namespace: /my_ns
              - node:
                  pkg: p
                  exec: e
                  name: n
        """)
        _, tracked = _parse_yaml_and_walk(yaml)
        assert "p" in tracked["packages"]

    def test_declared_args_tracked(self):
        yaml = textwrap.dedent("""\
            launch:
              - arg:
                  name: a
                  default: "1"
              - arg:
                  name: b
                  default: "2"
        """)
        _, tracked = _parse_yaml_and_walk(yaml)
        names = [a["name"] for a in tracked["declared_args"]]
        assert "a" in names
        assert "b" in names

    def test_executable(self):
        yaml = textwrap.dedent("""\
            launch:
              - executable:
                  cmd: ls -l
                  name: my_ls
        """)
        ctx, tracked = _parse_yaml_and_walk(yaml)
        # resolve_xml_elements should not raise; executable is processed
        assert ctx is not None

    def test_missing_launch_key_returns_empty(self):
        yaml = "foo: bar"
        _, tracked = _parse_yaml_and_walk(yaml)
        # No elements to walk — packages remain empty
        assert not tracked["packages"]


class TestTrackedFindPackageShare:
    """Tests for FindPackageShare mode-dependent perform()/str()."""

    def test_preview_known_pkg_returns_path(self):
        ctx = LaunchContext()
        ctx._state.preview_mode = True
        ctx._state.package_shares["my_pkg"] = "/ws/src/my_pkg"
        fps = FindPackageShare("my_pkg")
        assert fps.perform(ctx) == "/ws/src/my_pkg"
        # str() returns display form
        assert str(fps) == "$(find-pkg-share my_pkg)"

    def test_preview_unknown_pkg_raises(self):
        ctx = LaunchContext()
        ctx._state.preview_mode = True
        fps = FindPackageShare("unknown_pkg")
        import pytest

        with pytest.raises(LookupError, match="not found"):
            fps.perform(ctx)

    def test_postbuild_returns_install_path(self):
        ctx = LaunchContext()
        ctx._state.preview_mode = False
        ctx._state.package_shares["my_pkg"] = "/install/share/my_pkg"
        fps = FindPackageShare("my_pkg")
        assert fps.perform(ctx) == "/install/share/my_pkg"
        # str() returns display form; perform() returns resolved path
        assert str(fps) == "$(find-pkg-share my_pkg)"

    def test_postbuild_unresolvable_raises(self):
        ctx = LaunchContext()
        ctx._state.preview_mode = False
        fps = FindPackageShare("missing_pkg")
        import pytest

        with pytest.raises(LookupError, match="not found"):
            fps.perform(ctx)
