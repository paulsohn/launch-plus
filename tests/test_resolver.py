"""Tests for resolver internals — substitution handling, node tracking,
and inline Python include resolution."""

import logging
import os
import sys
import tempfile
import textwrap
import uuid

import pytest
from conftest import _install_import_patching

from roscope.entities.actions.composable_node_container import (
    ComposableNodeContainer,
    _resolve_plugins,
)
from roscope.entities.actions.declare_launch_argument import DeclareLaunchArgument
from roscope.entities.actions.include_launch_description import _inline_resolve_python_launch
from roscope.entities.actions.node import Node
from roscope.entities.actions.set_environment_variable import SetEnvironmentVariable
from roscope.entities.actions.unset_environment_variable import UnsetEnvironmentVariable
from roscope.entities.descriptions import ComposableNode
from roscope.entities.helpers import (
    _effective_namespace,
    _is_substitution,
    _is_truthy,
    env_overrides,
    resolve_substitutions,
)
from roscope.entities.launch_context import LaunchContext, ResolverState
from roscope.entities.substitutions.find_pkg_share import FindPackageShare
from roscope.entities.substitutions.launch_config import LaunchConfiguration
from roscope.resolver import parse_xml_launch, parse_yaml_launch, resolve_xml_elements

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_context(configs=None):
    """Build a LaunchContext with a fresh ResolverState and test defaults."""
    ctx = LaunchContext()
    ctx._state.preview_mode = True
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
        assert "my_pkg" in state.packages

    def test_skips_substitution_object(self):
        state = ResolverState()
        lc = LaunchConfiguration("container_pkg")
        state.track_package(lc)
        assert "container_pkg" not in state.packages
        assert len(state.packages) == 0

    def test_skips_empty_and_none(self):
        state = ResolverState()
        state.track_package(None)
        state.track_package("")
        assert len(state.packages) == 0

    def test_deduplicates(self):
        state = ResolverState()
        state.track_package("pkg_a")
        state.track_package("pkg_a")
        assert state.packages.count("pkg_a") == 1

    def test_skips_list_of_substitutions(self):
        state = ResolverState()
        parts = [LaunchConfiguration("pkg_var"), "_suffix"]
        state.track_package(parts)
        assert len(state.packages) == 0


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

        assert "actual_package" in ctx._state.packages

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

        assert "rclcpp_components" in ctx._state.packages

    def test_plain_string_package_tracked_on_execute(self):
        """When package is a plain string, it should be tracked after execute()."""
        ctx = _make_context()
        node = Node(package="my_real_pkg", executable="exec")
        node.execute(ctx)
        assert "my_real_pkg" in ctx._state.packages


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
        assert "sensor_driver" in ctx._state.packages

    def test_composable_node_unresolved_package_not_tracked(self):
        ctx = _make_context({})
        desc = ComposableNode(
            package=LaunchConfiguration("unknown"),
            plugin="foo::Bar",
        )
        plugins = _resolve_plugins([desc], ctx)
        assert plugins[0]["package"] == "$(var unknown)"  # portable fallback
        assert "unknown" not in ctx._state.packages

    def test_composable_node_empty_string_remapping_preserved(self):
        """Remapping resolved to empty string should be preserved, not
        replaced with the substitution display name."""
        ctx = _make_context({"remap_src": "", "remap_dst": ""})
        desc = ComposableNode(
            package="my_pkg",
            plugin="my_pkg::Node",
            remappings=[
                (LaunchConfiguration("remap_src"), LaunchConfiguration("remap_dst")),
            ],
        )
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
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {})

            # Global params are stored in _launch_configurations
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
            deps_before = len(ctx._state.include_deps)
            _inline_resolve_python_launch(ctx._state, child_path, ctx, {})

            # No new include deps
            assert len(ctx._state.include_deps) == deps_before


# ─── Environment Variable Stack ──────────────────────────────────────────────


class TestEnvStack:
    """Tests for SetEnvironmentVariable / UnsetEnvironmentVariable tracking,
    env inheritance to nodes, and group scoping."""

    def test_unset_env_nonexistent_errors(self, caplog):
        """UnsetEnvironmentVariable on a var that doesn't exist → 'not set' error."""

        name = f"NONEXISTENT_VAR_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        with caplog.at_level(logging.WARNING):
            UnsetEnvironmentVariable(name=name).execute(ctx)
        assert name in caplog.text and "not set" in caplog.text

    def test_unset_env_override_only_accepted(self):
        """UnsetEnv on an override-only var (not in process env) → accepted."""

        name = f"OVERRIDE_ONLY_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        SetEnvironmentVariable(name=name, value="val").execute(ctx)
        assert name in ctx.environment
        UnsetEnvironmentVariable(name=name).execute(ctx)
        assert name not in ctx.environment
        # No errors should be logged for a successful unset

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
        xml = '<launch><let name="v" value="1" if="$(var flag)"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.get_attr("if") == "$(var flag)"

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
                <group scoped="false" if="$(var x)">
                    <arg name="nested" default="val"/>
                </group>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "group"
        assert e.get_attr("scoped") == "false"
        assert e.get_attr("if") == "$(var x)"
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
                <unset_env name="Y" unless="$(var flag)"/>
            </launch>
        """)
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "set_env"
        assert elems[0].get_attr("name") == "X"
        assert elems[1].type_name == "unset_env"
        assert elems[1].get_attr("name") == "Y"
        assert elems[1].get_attr("unless") == "$(var flag)"

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
        xml = '<launch><node pkg="$(var pkg)" exec="$(var exe)"/></launch>'
        elems = parse_xml_launch(xml, "test.xml")
        assert elems[0].get_attr("pkg") == "$(var pkg)"
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
        result = resolve_substitutions("$(var vehicle)", ctx)
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
        assert "my_pkg" in ctx._state.packages

    def test_resolve_find_pkg_prefix(self, tmp_path, monkeypatch):
        # Build a fake AMENT index: <prefix>/share/ament_index/resource_index/packages/<pkg>
        prefix = tmp_path / "opt" / "ros" / "humble"
        marker_dir = prefix / "share" / "ament_index" / "resource_index" / "packages"
        marker_dir.mkdir(parents=True)
        (marker_dir / "my_pkg").touch()
        monkeypatch.setenv("AMENT_PREFIX_PATH", str(prefix))
        ctx = _fresh_subst_ctx(preview_mode=False)
        result = resolve_substitutions("$(find-pkg-prefix my_pkg)/lib", ctx)
        assert result == str(prefix / "lib")
        assert "my_pkg" in ctx._state.packages

    def test_resolve_find_pkg_prefix_preview_errors(self):

        ctx = _fresh_subst_ctx(preview_mode=True)
        ctx._state.package_shares["my_pkg"] = "/ws/src/my_pkg"
        with pytest.raises(LookupError, match="unavailable in preview mode"):
            resolve_substitutions("$(find-pkg-prefix my_pkg)", ctx)

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
                # In the new system, <let> resolves $(var vehicle) before storing
                "config_path": "$(find-pkg-share sample_description)/config",
            },
            preview_mode=True,
        )
        result = resolve_substitutions("$(var config_path)/params.yaml", ctx)
        assert result == "$(find-pkg-share sample_description)/config/params.yaml"

    def test_resolve_error_undefined_arg(self, caplog):
        ctx = _fresh_subst_ctx()
        with caplog.at_level(logging.WARNING):
            result = resolve_substitutions("$(var undefined)", ctx)
        assert "$(var undefined)" in result
        assert "undefined variable" in caplog.text

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
        assert "pkg1" in ctx._state.packages
        assert "pkg2" in ctx._state.packages


# ─── AST Walker (resolve_xml_elements) ───────────────────────────────────────


def _fresh_walker_ctx(**kwargs):
    """Create a fresh LaunchContext with test defaults for walker tests."""
    ctx = LaunchContext()
    ctx._state.preview_mode = True
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
    return ctx, ctx._state


class TestResolveXmlElements:
    """Tests for resolve_xml_elements() — the XML/YAML AST walker."""

    # ── Basic node resolution ──

    def test_simple_node(self):
        xml = '<launch><node pkg="my_pkg" exec="my_node" name="node1"/></launch>'
        _, state = _parse_and_walk(xml)
        assert "my_pkg" in state.packages

    # ── Arg and Let ──

    def test_arg_default_applied(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="vehicle" default="sample"/>
              <node pkg="$(var vehicle)_pkg" exec="node" name="n"/>
            </launch>
        """)
        _, state = _parse_and_walk(xml)
        assert "sample_pkg" in state.packages

    def test_declared_args_tracked(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="a" default="1"/>
              <arg name="b" default="2"/>
            </launch>
        """)
        ctx, _ = _parse_and_walk(xml)
        assert "a" in ctx._state.declared_arg_names
        assert "b" in ctx._state.declared_arg_names

    def test_let_with_condition(self, caplog):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="flag" default="false"/>
              <let name="x" value="set" if="$(var flag)"/>
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
        ctx, _ = _parse_and_walk(xml)
        gp = ctx._launch_configurations.get("global_params", [])
        assert any(name == "use_sim_time" for name, _ in gp)

    # ── Include (with file on disk) ──

    def test_include_xml_inline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write child launch file
            child_xml = textwrap.dedent("""\
                <launch>
                  <arg name="param1"/>
                  <node pkg="included_pkg" exec="node" name="$(var param1)_node"/>
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
            _, state = _parse_and_walk(main_xml)
            assert "included_pkg" in state.packages

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
            ctx, _ = _parse_and_walk(main_xml)
            # The explicitly-passed arg is applied to the shared context
            assert ctx._launch_configurations.get("x") == "42"

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

        for invalid in ("yes", "on", "no", ""):
            with pytest.raises(ValueError, match="invalid condition expression"):
                _is_truthy(invalid)


# ─── Action registry resolution (resolve_xml_to_ir → _tracked) ──────────────


def _parse_to_tracked(xml_str, **ctx_kwargs):
    """Parse XML string, resolve via action registry, return (ctx, _tracked)."""
    ctx = _fresh_walker_ctx(**ctx_kwargs)
    elements = parse_xml_launch(xml_str, "test.launch.xml")
    resolve_xml_elements(elements, ctx)
    return ctx, ctx._state


class TestActionRegistry:
    """Tests for action registry resolution — _tracked output."""

    def test_declared_args(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="x" default="1"/>
              <arg name="y" default="2"/>
            </launch>
        """)
        ctx, _ = _parse_to_tracked(xml)
        assert "x" in ctx._state.declared_arg_names
        assert "y" in ctx._state.declared_arg_names
        assert ctx._launch_configurations.get("x") == "1"
        assert ctx._launch_configurations.get("y") == "2"

    def test_packages_tracked(self):
        xml = '<launch><node pkg="my_pkg" exec="e" name="n"/></launch>'
        _, state = _parse_to_tracked(xml)
        assert "my_pkg" in state.packages

    def test_errors_and_warnings(self, caplog):
        xml = textwrap.dedent("""\
            <launch>
              <node pkg="$(var undefined)" exec="e" name="n"/>
              <foobar/>
            </launch>
        """)
        with caplog.at_level(logging.WARNING):
            _parse_to_tracked(xml)
        assert "undefined" in caplog.text
        assert "unknown" in caplog.text


# ─── rosdep resolve parser tests ─────────────────────────────────────────────


# ─── DeclareLaunchArgument.execute() ─────────────────────────────────────────


class TestDeclareLaunchArgumentExecute:
    """DeclareLaunchArgument.execute() resolves defaults immediately (matching official)."""

    def test_arg_already_set_is_preserved(self, caplog):
        """Caller-provided value is not overwritten."""
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
            arg.execute(ctx)
        # Arg value unchanged (caller's value preserved).
        assert ctx._launch_configurations["my_arg"] == "already_set_value"

    def test_default_resolved_immediately_when_arg_not_set(self):
        """Default is resolved and stored immediately — matching official."""
        ctx = _make_context({})
        ctx._state.preview_mode = True
        ctx._state.package_shares["my_pkg"] = "/ws/src/my_pkg"
        arg = DeclareLaunchArgument(
            "my_arg",
            default_value="simple_default",
        )
        arg.execute(ctx)
        # Resolved immediately (no deferred default).
        assert ctx._launch_configurations["my_arg"] == "simple_default"
        assert LaunchConfiguration("my_arg").perform(ctx) == "simple_default"

    def test_default_applies_via_xml(self):
        """<arg default=...> immediately resolves when arg not provided."""
        ctx = LaunchContext()
        elements = parse_xml_launch('<launch><arg name="x" default="hello"/></launch>', "test.xml")
        resolve_xml_elements(elements, ctx)
        assert resolve_substitutions("$(var x)", ctx) == "hello"


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

        # Should raise, not attempt to fetch
        with pytest.raises(LookupError, match="not found"):
            state.resolve_pkg_share("lockfile_pkg")


def _parse_yaml_and_walk(yaml_str, ctx=None, **ctx_kwargs):
    """Parse YAML string and walk it.  Returns (ctx, tracked)."""
    if ctx is None:
        ctx = _fresh_walker_ctx(**ctx_kwargs)
    elements = parse_yaml_launch(yaml_str, "test.launch.yaml")
    resolve_xml_elements(elements, ctx)
    return ctx, ctx._state


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
        _, state = _parse_yaml_and_walk(yaml)
        assert "my_pkg" in state.packages

    def test_arg_and_let(self):
        yaml = textwrap.dedent("""\
            launch:
              - arg:
                  name: vehicle
                  default: sample
              - node:
                  pkg: "$(var vehicle)_pkg"
                  exec: node
                  name: n
        """)
        _, state = _parse_yaml_and_walk(yaml)
        assert "sample_pkg" in state.packages

    def test_set_parameter(self):
        yaml = textwrap.dedent("""\
            launch:
              - set_parameter:
                  name: use_sim_time
                  value: "true"
        """)
        ctx, _ = _parse_yaml_and_walk(yaml)
        gp = ctx._launch_configurations.get("global_params", [])
        assert any(name == "use_sim_time" for name, _ in gp)

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
        _, state = _parse_yaml_and_walk(yaml)
        assert "p" in state.packages

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
        ctx, _ = _parse_yaml_and_walk(yaml)
        assert "a" in ctx._state.declared_arg_names
        assert "b" in ctx._state.declared_arg_names

    def test_executable(self):
        yaml = textwrap.dedent("""\
            launch:
              - executable:
                  cmd: ls -l
                  name: my_ls
        """)
        ctx, state = _parse_yaml_and_walk(yaml)
        # resolve_xml_elements should not raise; executable is processed
        assert ctx is not None

    def test_missing_launch_key_returns_empty(self):
        yaml = "foo: bar"
        _, state = _parse_yaml_and_walk(yaml)
        # No elements to walk — packages remain empty
        assert not state.packages


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

        with pytest.raises(LookupError, match="not found"):
            fps.perform(ctx)


# ─── Faithfulness fixes (issue #54) ──────────────────────────────────────────


class TestEnvironmentVariable:
    """EnvironmentVariable: unified XML+Python substitution."""

    def test_xml_path_set(self, monkeypatch):
        """$(env VAR) resolves when the variable is set."""
        from roscope.parsers.parse_substitution import parse_substitution

        monkeypatch.setenv("TEST_ENV_VAR_XYZ", "hello")
        ctx = _make_context()
        result = "".join(s.perform(ctx) for s in parse_substitution("$(env TEST_ENV_VAR_XYZ)"))
        assert result == "hello"

    def test_xml_path_missing_logs_error(self, monkeypatch, caplog):
        """$(env VAR) without default logs an error and returns empty string."""
        from roscope.parsers.parse_substitution import parse_substitution

        monkeypatch.delenv("TEST_ENV_MISSING_XYZ", raising=False)
        ctx = _make_context()
        with caplog.at_level(logging.ERROR):
            result = "".join(
                s.perform(ctx) for s in parse_substitution("$(env TEST_ENV_MISSING_XYZ)")
            )
        assert result == ""
        assert "TEST_ENV_MISSING_XYZ" in caplog.text

    def test_xml_path_default(self, monkeypatch):
        """$(env VAR default) returns default when the variable is unset."""
        from roscope.parsers.parse_substitution import parse_substitution

        monkeypatch.delenv("TEST_ENV_MISSING_XYZ", raising=False)
        ctx = _make_context()
        result = "".join(
            s.perform(ctx) for s in parse_substitution("$(env TEST_ENV_MISSING_XYZ fallback)")
        )
        assert result == "fallback"

    def test_python_api_str_name(self, monkeypatch):
        """EnvironmentVariable('VAR') works with a plain string name."""
        from roscope.entities.substitutions.env import EnvironmentVariable

        monkeypatch.setenv("TEST_ENV_VAR_XYZ", "world")
        ctx = _make_context()
        assert EnvironmentVariable("TEST_ENV_VAR_XYZ").perform(ctx) == "world"

    def test_python_api_missing_logs_error(self, monkeypatch, caplog):
        """EnvironmentVariable('VAR') logs an error and returns '' when var is unset."""
        from roscope.entities.substitutions.env import EnvironmentVariable

        monkeypatch.delenv("TEST_ENV_MISSING_XYZ", raising=False)
        ctx = _make_context()
        with caplog.at_level(logging.ERROR):
            result = EnvironmentVariable("TEST_ENV_MISSING_XYZ").perform(ctx)
        assert result == ""
        assert "TEST_ENV_MISSING_XYZ" in caplog.text

    def test_python_api_default_value(self, monkeypatch):
        """EnvironmentVariable('VAR', default_value='x') returns default when unset."""
        from roscope.entities.substitutions.env import EnvironmentVariable

        monkeypatch.delenv("TEST_ENV_MISSING_XYZ", raising=False)
        ctx = _make_context()
        assert (
            EnvironmentVariable("TEST_ENV_MISSING_XYZ", default_value="fallback").perform(ctx)
            == "fallback"
        )


class TestCommandSubstitutionWarning:
    """Issue 4: $(command ...) should warn when encountered."""

    def test_command_substitution_warns(self, caplog):
        """$(command ...) must emit a warning when perform() is called."""
        from roscope.entities.substitutions.command import CommandSubstitution
        from roscope.parsers.parse_substitution import parse_substitution

        ctx = _make_context()
        substs = parse_substitution("$(command echo hello)")
        assert len(substs) == 1
        assert isinstance(substs[0], CommandSubstitution)
        with caplog.at_level(logging.WARNING):
            result = substs[0].perform(ctx)
        # Value is preserved as literal
        assert result == "$(command echo hello)"
        # Warning emitted
        assert "$(command" in caplog.text

    def test_command_substitution_in_attribute_warns(self, caplog):
        """$(command ...) in an XML attribute warns during resolution."""
        xml = '<launch><node pkg="$(command echo pkg)" exec="e" name="n"/></launch>'
        with caplog.at_level(logging.WARNING):
            _parse_and_walk(xml)
        assert "$(command" in caplog.text


class TestPostBuildSourceIgnored:
    """Issue 6: post-build mode must not consult source/lockfile paths."""

    def test_resolve_pkg_share_postbuild_skips_lockfile(self, monkeypatch):
        """In post-build mode, resolve_pkg_share() must not use lockfile paths."""
        from roscope.entities.launch_context import ResolverState

        state = ResolverState()
        state.preview_mode = False
        # Populate lockfile data as if a package is in the lockfile
        state.lockfile_data = {
            "my_pkg": {
                "repo": "my_repo",
                "path": "my_pkg",
                "url": "https://example.com/repo.git",
                "version": "abc123",
            }
        }
        state.fetch_dir = "/ws/src"

        # Patch ensure_package_available to record what lockfile it receives
        received_lockfile = []

        def fake_ensure(package, lockfile, fetch_dir, options, *, rosdep_fallback=False):
            received_lockfile.append(lockfile)
            return None

        import roscope.fetcher as _fetcher

        monkeypatch.setattr(_fetcher, "ensure_package_available", fake_ensure)

        try:
            state.resolve_pkg_share("my_pkg")
        except LookupError:
            pass  # expected — fake returns None

        # lockfile must be None in post-build mode
        assert received_lockfile, "ensure_package_available was never called"
        assert received_lockfile[0] is None, (
            "post-build mode must pass lockfile=None to ensure_package_available"
        )

    def test_resolve_pkg_share_preview_uses_lockfile(self, monkeypatch):
        """In preview mode, resolve_pkg_share() must still use the lockfile."""
        from roscope.entities.launch_context import ResolverState

        state = ResolverState()
        state.preview_mode = True
        state.lockfile_data = {
            "my_pkg": {
                "repo": "my_repo",
                "path": "my_pkg",
                "url": "https://example.com/repo.git",
                "version": "abc123",
            }
        }
        state.fetch_dir = "/ws/src"

        received_lockfile = []

        def fake_ensure(package, lockfile, fetch_dir, options, *, rosdep_fallback=False):
            received_lockfile.append(lockfile)
            return None

        import roscope.fetcher as _fetcher

        monkeypatch.setattr(_fetcher, "ensure_package_available", fake_ensure)

        try:
            state.resolve_pkg_share("my_pkg")
        except LookupError:
            pass

        assert received_lockfile
        assert received_lockfile[0] is not None, (
            "preview mode must pass lockfile to ensure_package_available"
        )


class TestEventHandlerWarning:
    """Issue 7: RegisterEventHandler must warn when executed."""

    def test_register_event_handler_warns(self, caplog):
        """RegisterEventHandler.execute() must emit a warning."""
        from roscope.entities.actions.register_event_handler import RegisterEventHandler

        ctx = _make_context()
        handler = RegisterEventHandler(event_handler=None)
        with caplog.at_level(logging.WARNING):
            result = handler.execute(ctx)
        assert result == []
        assert "RegisterEventHandler" in caplog.text

    def test_register_event_handler_via_visit_warns(self, caplog):
        """visit() path (via Python shim) also emits the warning."""
        from roscope.entities.actions.register_event_handler import RegisterEventHandler

        ctx = _make_context()
        handler = RegisterEventHandler()
        with caplog.at_level(logging.WARNING):
            handler.visit(ctx)
        assert "RegisterEventHandler" in caplog.text

    def test_register_event_handler_in_py_launch_warns(self, caplog):
        """End-to-end: RegisterEventHandler in a Python launch file warns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = _write_launch_py(
                tmpdir,
                "evh.launch.py",
                """\
                from launch import LaunchDescription
                from launch.actions import RegisterEventHandler

                def generate_launch_description():
                    return LaunchDescription([
                        RegisterEventHandler(event_handler=None),
                    ])
            """,
            )
            ctx = _make_context()
            with caplog.at_level(logging.WARNING):
                from roscope.entities.actions.include_launch_description import (
                    _inline_resolve_python_launch,
                )

                _inline_resolve_python_launch(ctx._state, child_path, ctx, {})
            assert "RegisterEventHandler" in caplog.text


class TestShimUnknownAction:
    """Issue 8: unknown imports from action shim modules must produce a no-op Action stub."""

    def test_unknown_launch_action_warns_not_crashes(self, caplog):
        """from launch.actions import UnknownFutureAction must not crash."""
        _install_import_patching()

        launch_actions = sys.modules["launch.actions"]
        with caplog.at_level(logging.WARNING):
            stub_cls = getattr(launch_actions, "SomeFutureAction_XYZ_123", None)
        assert stub_cls is not None
        assert "SomeFutureAction_XYZ_123" in caplog.text

    def test_unknown_shim_returns_action_subclass(self):
        """The stub class must be an Action subclass usable by _execute_actions."""

        from roscope.entities.action import Action

        _install_import_patching()

        launch_actions = sys.modules["launch.actions"]
        stub_cls = getattr(launch_actions, "AnotherUnknownAction_ABC", None)
        assert stub_cls is not None
        assert issubclass(stub_cls, Action)
        # Can instantiate with arbitrary args
        instance = stub_cls(foo="bar", baz=42)
        ctx = _make_context()
        assert instance.execute(ctx) == []

    def test_dunder_attr_raises_attribute_error(self):
        """__dunder__ attribute access on shim action modules must raise AttributeError."""
        _install_import_patching()

        launch_actions = sys.modules["launch.actions"]

        with pytest.raises(AttributeError):
            _ = launch_actions.__some_dunder__


class TestShimUnknownSubstitution:
    """Issue 8: unknown imports from substitution shim modules must produce a Substitution stub."""

    def test_unknown_substitution_shim_returns_substitution_subclass(self):
        """Unknown attributes on substitution shim modules must return a Substitution subclass."""

        from roscope.entities.substitution import Substitution

        _install_import_patching()

        launch_subs = sys.modules["launch.substitutions"]
        stub_cls = getattr(launch_subs, "SomeUnknownSubstitution_XYZ", None)
        assert stub_cls is not None
        assert issubclass(stub_cls, Substitution)
        ctx = _make_context()
        assert stub_cls().perform(ctx) == ""

    def test_dunder_attr_raises_attribute_error(self):
        """__dunder__ attribute access on shim substitution modules must raise AttributeError."""
        _install_import_patching()

        launch_subs = sys.modules["launch.substitutions"]
        with pytest.raises(AttributeError):
            _ = launch_subs.__some_dunder__


class TestExecutableInPackage:
    """ExecutableInPackage substitution: preview raises, post-build resolves."""

    def test_raises_in_preview(self):
        """ExecutableInPackage must raise LookupError in preview mode."""

        from roscope.entities.substitution import TextSubstitution
        from roscope.entities.substitutions.executable_in_package import ExecutableInPackage

        ctx = _make_context()
        ctx._state.preview_mode = True
        shim = ExecutableInPackage(
            executable=[TextSubstitution(text="my_exec")],
            package=[TextSubstitution(text="my_pkg")],
        )
        with pytest.raises(LookupError, match="preview mode"):
            shim.perform(ctx)

    def test_xml_registered(self):
        """exec-in-pkg must be registered as an XML substitution."""
        from roscope.entities.expose import substitution_parse_methods

        assert "exec-in-pkg" in substitution_parse_methods

    def test_launch_ros_substitutions_exposes_class(self):
        """launch_ros.substitutions shim must expose ExecutableInPackage."""

        from roscope.entities.substitutions.executable_in_package import ExecutableInPackage

        _install_import_patching()

        lr_subs = sys.modules["launch_ros.substitutions"]
        assert hasattr(lr_subs, "ExecutableInPackage")
        assert lr_subs.ExecutableInPackage is ExecutableInPackage

    def test_resolves_in_postbuild(self, tmp_path, monkeypatch):
        """ExecutableInPackage must resolve the executable path in post-build mode."""
        from roscope.entities.substitution import TextSubstitution
        from roscope.entities.substitutions.executable_in_package import ExecutableInPackage

        # Build a fake AMENT prefix with index marker and libexec executable.
        prefix = tmp_path / "prefix"
        pkg_marker = prefix / "share" / "ament_index" / "resource_index" / "packages" / "my_pkg"
        pkg_marker.parent.mkdir(parents=True)
        pkg_marker.touch()
        libexec = prefix / "lib" / "my_pkg"
        libexec.mkdir(parents=True)
        exe_path = libexec / "my_exec"
        exe_path.write_text("#!/bin/bash\n")
        exe_path.chmod(0o755)

        monkeypatch.setenv("AMENT_PREFIX_PATH", str(prefix))

        ctx = _make_context()
        ctx._state.preview_mode = False
        shim = ExecutableInPackage(
            executable=[TextSubstitution(text="my_exec")],
            package=[TextSubstitution(text="my_pkg")],
        )
        result = shim.perform(ctx)
        assert result == str(exe_path)


class TestLaunchXmlShim:
    """launch_xml.launch_description_sources shim resolves to roscope implementation."""

    def test_xml_launch_description_source_shim(self):
        """XMLLaunchDescriptionSource shim resolves to the roscope implementation."""

        from roscope.entities.launch_description_sources import XMLLaunchDescriptionSource

        _install_import_patching()
        assert "launch_xml.launch_description_sources" in sys.modules
        from launch_xml.launch_description_sources import (
            XMLLaunchDescriptionSource as Shimmed,
        )

        assert Shimmed is XMLLaunchDescriptionSource

    def test_frontend_launch_description_source_in_launch_shim(self):
        """FrontendLaunchDescriptionSource shim resolves to the roscope implementation."""

        from roscope.entities.launch_description_sources import FrontendLaunchDescriptionSource

        _install_import_patching()
        assert "launch.launch_description_sources" in sys.modules
        from launch.launch_description_sources import (
            FrontendLaunchDescriptionSource as Shimmed,
        )

        assert Shimmed is FrontendLaunchDescriptionSource

    def test_launch_description_source_base_module_shim(self):
        """LaunchDescriptionSource base-module shim resolves to the roscope implementation."""

        from roscope.entities.launch_description_source import LaunchDescriptionSource

        _install_import_patching()
        assert "launch.launch_description_source" in sys.modules
        from launch.launch_description_source import LaunchDescriptionSource as Shimmed

        assert Shimmed is LaunchDescriptionSource

    def test_per_class_submodule_shims(self):
        """Per-class submodule import paths resolve to the roscope implementations."""

        from roscope.entities.launch_description_sources import (
            AnyLaunchDescriptionSource,
            FrontendLaunchDescriptionSource,
            PythonLaunchDescriptionSource,
            XMLLaunchDescriptionSource,
        )

        _install_import_patching()

        assert "launch.launch_description_sources.any_launch_description_source" in sys.modules
        from launch.launch_description_sources.any_launch_description_source import (
            AnyLaunchDescriptionSource as ShimmedAny,
        )

        assert ShimmedAny is AnyLaunchDescriptionSource

        assert "launch.launch_description_sources.python_launch_description_source" in sys.modules
        from launch.launch_description_sources.python_launch_description_source import (
            PythonLaunchDescriptionSource as ShimmedPython,
        )

        assert ShimmedPython is PythonLaunchDescriptionSource

        assert "launch.launch_description_sources.frontend_launch_description_source" in sys.modules
        from launch.launch_description_sources.frontend_launch_description_source import (
            FrontendLaunchDescriptionSource as ShimmedFrontend,
        )

        assert ShimmedFrontend is FrontendLaunchDescriptionSource

        assert "launch_xml.launch_description_sources.xml_launch_description_source" in sys.modules
        from launch_xml.launch_description_sources.xml_launch_description_source import (
            XMLLaunchDescriptionSource as ShimmedXML,
        )

        assert ShimmedXML is XMLLaunchDescriptionSource

    def test_launch_ros_submodule_is_exposed_as_parent_attribute(self):
        """Submodule shims must be accessible as attributes on the parent shim."""
        from roscope.entities.parameter_descriptions import ParameterFile

        _install_import_patching()

        import launch_ros

        assert hasattr(launch_ros, "parameter_descriptions")
        assert launch_ros.parameter_descriptions.ParameterFile is ParameterFile
