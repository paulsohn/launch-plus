"""Tests for py_resolver internals — substitution handling, node tracking,
and inline Python include resolution."""

import os
import tempfile
import textwrap

from launch_plus import py_resolver as R

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_context(configs=None):
    """Build a _StubLaunchContext with the given launch configurations."""
    ctx = R._StubLaunchContext()
    ctx._launch_configurations = dict(configs or {})
    return ctx


def _write_launch_py(directory, filename, body):
    """Write a minimal Python launch file into *directory*."""
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        f.write(textwrap.dedent(body))
    return path


# ─── _LaunchConfiguration ────────────────────────────────────────────────────


class TestLaunchConfiguration:
    def test_perform_returns_value_when_set(self):
        lc = R._LaunchConfiguration("my_var")
        ctx = _make_context({"my_var": "hello"})
        assert lc.perform(ctx) == "hello"

    def test_perform_returns_none_when_unset(self):
        lc = R._LaunchConfiguration("missing_var")
        ctx = _make_context({})
        assert lc.perform(ctx) is None

    def test_perform_returns_default_when_unset_but_default_given(self):
        lc = R._LaunchConfiguration("missing_var", default="fallback")
        ctx = _make_context({})
        assert lc.perform(ctx) == "fallback"

    def test_perform_prefers_context_over_default(self):
        lc = R._LaunchConfiguration("my_var", default="fallback")
        ctx = _make_context({"my_var": "from_context"})
        assert lc.perform(ctx) == "from_context"

    def test_perform_returns_none_without_context(self):
        lc = R._LaunchConfiguration("x")
        assert lc.perform(None) is None

    def test_str_returns_variable_name(self):
        lc = R._LaunchConfiguration("pkg_name")
        assert str(lc) == "pkg_name"


# ─── _is_substitution ────────────────────────────────────────────────────────


class TestIsSubstitution:
    def test_launch_configuration_is_substitution(self):
        assert R._is_substitution(R._LaunchConfiguration("x"))

    def test_string_is_not_substitution(self):
        assert not R._is_substitution("rclcpp_components")

    def test_none_is_not_substitution(self):
        assert not R._is_substitution(None)

    def test_list_of_substitutions_is_substitution(self):
        parts = [R._LaunchConfiguration("x"), "_suffix"]
        assert R._is_substitution(parts)

    def test_list_of_plain_strings_is_not_substitution(self):
        assert not R._is_substitution(["hello", "world"])

    def test_empty_list_is_not_substitution(self):
        assert not R._is_substitution([])

    def test_tuple_of_substitutions_is_substitution(self):
        parts = (R._LaunchConfiguration("x"),)
        assert R._is_substitution(parts)


# ─── _track_package ──────────────────────────────────────────────────────────


class TestTrackPackage:
    def test_tracks_plain_string(self):
        R._track_package("my_pkg")
        assert "my_pkg" in R._tracked["packages"]

    def test_skips_substitution_object(self):
        lc = R._LaunchConfiguration("container_pkg")
        R._track_package(lc)
        assert "container_pkg" not in R._tracked["packages"]
        assert len(R._tracked["packages"]) == 0

    def test_skips_empty_and_none(self):
        R._track_package(None)
        R._track_package("")
        assert len(R._tracked["packages"]) == 0

    def test_deduplicates(self):
        R._track_package("pkg_a")
        R._track_package("pkg_a")
        assert R._tracked["packages"].count("pkg_a") == 1

    def test_skips_list_of_substitutions(self):
        parts = [R._LaunchConfiguration("pkg_var"), "_suffix"]
        R._track_package(parts)
        assert len(R._tracked["packages"]) == 0


# ─── _resolve_substitution ───────────────────────────────────────────────────


class TestResolveSubstitution:
    def test_resolves_launch_configuration(self):
        lc = R._LaunchConfiguration("my_var")
        ctx = _make_context({"my_var": "resolved_value"})
        assert R._resolve_substitution(lc, ctx) == "resolved_value"

    def test_unresolved_falls_back_to_str(self):
        lc = R._LaunchConfiguration("missing")
        ctx = _make_context({})
        # perform() returns None → _resolve_substitution falls back to str(lc)
        assert R._resolve_substitution(lc, ctx) == "missing"

    def test_plain_string_passthrough(self):
        assert R._resolve_substitution("hello", None) == "hello"

    def test_none_returns_none(self):
        assert R._resolve_substitution(None, None) is None

    def test_list_of_substitutions(self):
        parts = [
            R._LaunchConfiguration("prefix"),
            "_",
            R._LaunchConfiguration("suffix"),
        ]
        ctx = _make_context({"prefix": "foo", "suffix": "bar"})
        assert R._resolve_substitution(parts, ctx) == "foo_bar"

    def test_list_with_unresolved_element(self):
        parts = [
            R._LaunchConfiguration("resolved_var"),
            "/",
            R._LaunchConfiguration("unresolved_var"),
        ]
        ctx = _make_context({"resolved_var": "abc"})
        # Unresolved element falls back to str(lc) = variable name
        assert R._resolve_substitution(parts, ctx) == "abc/unresolved_var"

    def test_ex_returns_not_fallback_when_resolved(self):
        lc = R._LaunchConfiguration("my_var")
        ctx = _make_context({"my_var": "resolved_value"})
        value, is_fallback = R._resolve_substitution_ex(lc, ctx)
        assert value == "resolved_value"
        assert is_fallback is False

    def test_ex_returns_fallback_when_unresolved(self):
        lc = R._LaunchConfiguration("missing")
        ctx = _make_context({})
        value, is_fallback = R._resolve_substitution_ex(lc, ctx)
        assert value == "missing"
        assert is_fallback is True

    def test_ex_list_fallback_when_any_unresolved(self):
        parts = [R._LaunchConfiguration("a"), "_", R._LaunchConfiguration("b")]
        ctx = _make_context({"a": "resolved"})
        value, is_fallback = R._resolve_substitution_ex(parts, ctx)
        assert value == "resolved_b"
        assert is_fallback is True

    def test_ex_list_not_fallback_when_all_resolved(self):
        parts = [R._LaunchConfiguration("a"), "_", R._LaunchConfiguration("b")]
        ctx = _make_context({"a": "foo", "b": "bar"})
        value, is_fallback = R._resolve_substitution_ex(parts, ctx)
        assert value == "foo_bar"
        assert is_fallback is False

    def test_ex_plain_string_not_fallback(self):
        value, is_fallback = R._resolve_substitution_ex("hello", None)
        assert value == "hello"
        assert is_fallback is False

    def test_ex_tuple_resolves_same_as_list(self):
        parts = (R._LaunchConfiguration("a"), "_", R._LaunchConfiguration("b"))
        ctx = _make_context({"a": "foo", "b": "bar"})
        value, is_fallback = R._resolve_substitution_ex(parts, ctx)
        assert value == "foo_bar"
        assert is_fallback is False

    def test_ex_tuple_fallback_when_unresolved(self):
        parts = (R._LaunchConfiguration("a"), "_suffix")
        ctx = _make_context({})
        value, is_fallback = R._resolve_substitution_ex(parts, ctx)
        assert value == "a_suffix"
        assert is_fallback is True

    def test_ex_list_fallback_when_no_context(self):
        """When context is None, substitutions with perform() should be fallback."""
        parts = [R._LaunchConfiguration("x"), "_literal"]
        value, is_fallback = R._resolve_substitution_ex(parts, None)
        assert value == "x_literal"
        assert is_fallback is True


# ─── Node deferred resolution ────────────────────────────────────────────────


class TestNodeDeferredResolution:
    def test_tracked_node_resolves_package_substitution(self):
        """When package is a LaunchConfiguration, _resolve_node_details should
        resolve it to the concrete value and track the resolved package."""
        ctx = _make_context({"my_pkg_var": "actual_package"})
        node = R._TrackedNode(
            package=R._LaunchConfiguration("my_pkg_var"),
            executable="my_exec",
        )
        # Before resolution: str(LaunchConfiguration) = variable name
        assert R._tracked["nodes"][node._idx]["package"] == "my_pkg_var"
        assert "my_pkg_var" not in R._tracked["packages"]  # not tracked eagerly

        R._resolve_node_details(node, ctx)

        assert R._tracked["nodes"][node._idx]["package"] == "actual_package"
        assert "actual_package" in R._tracked["packages"]

    def test_tracked_node_unresolved_package_stays_as_name(self):
        """When the LaunchConfiguration cannot be resolved (not in context),
        the entry keeps the variable name but does NOT track it as a package."""
        ctx = _make_context({})
        node = R._TrackedNode(
            package=R._LaunchConfiguration("unknown_pkg"),
            executable="exec",
        )
        R._resolve_node_details(node, ctx)

        # The entry shows the variable name (display fallback from str(lc))
        assert R._tracked["nodes"][node._idx]["package"] == "unknown_pkg"
        # But it must NOT be tracked as a real package dependency
        assert "unknown_pkg" not in R._tracked["packages"]

    def test_tracked_container_resolves_all_fields(self):
        ctx = _make_context(
            {
                "pkg": "rclcpp_components",
                "exe": "component_container_mt",
                "cname": "my_container",
            }
        )
        container = R._TrackedComposableNodeContainer(
            package=R._LaunchConfiguration("pkg"),
            executable=R._LaunchConfiguration("exe"),
            name=R._LaunchConfiguration("cname"),
        )
        R._resolve_node_details(container, ctx)

        entry = R._tracked["nodes"][container._idx]
        assert entry["package"] == "rclcpp_components"
        assert entry["executable"] == "component_container_mt"
        assert entry["name"] == "my_container"
        assert "rclcpp_components" in R._tracked["packages"]

    def test_plain_string_package_tracked_eagerly(self):
        """When package is a plain string, it should be tracked at construction."""
        R._TrackedNode(package="my_real_pkg", executable="exec")
        assert "my_real_pkg" in R._tracked["packages"]


# ─── Composable plugin deferred resolution ────────────────────────────────────


class TestComposablePluginResolution:
    def test_composable_node_resolves_package(self):
        ctx = _make_context({"plugin_pkg": "sensor_driver"})
        desc = R._TrackedComposableNode(
            package=R._LaunchConfiguration("plugin_pkg"),
            plugin="sensor_driver::SensorNode",
            name="sensor",
        )
        plugins = R._resolve_composable_plugins([desc], ctx)
        assert len(plugins) == 1
        assert plugins[0]["package"] == "sensor_driver"
        assert "sensor_driver" in R._tracked["packages"]

    def test_composable_node_unresolved_package_not_tracked(self):
        ctx = _make_context({})
        desc = R._TrackedComposableNode(
            package=R._LaunchConfiguration("unknown"),
            plugin="foo::Bar",
        )
        plugins = R._resolve_composable_plugins([desc], ctx)
        assert plugins[0]["package"] == "unknown"  # display fallback
        assert "unknown" not in R._tracked["packages"]

    def test_composable_node_empty_string_remapping_preserved(self):
        """Remapping resolved to empty string should be preserved, not
        replaced with the substitution display name."""
        ctx = _make_context({"remap_src": "", "remap_dst": ""})
        desc = R._TrackedComposableNode(
            package="my_pkg",
            plugin="my_pkg::Node",
        )
        desc._raw_remappings = [
            (R._LaunchConfiguration("remap_src"), R._LaunchConfiguration("remap_dst")),
        ]
        plugins = R._resolve_composable_plugins([desc], ctx)
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
            R._inline_resolve_python_launch(child_path, ctx, {}, depth=1)

            assert ctx._launch_configurations["child_var"] == "child_value"
            assert ctx._launch_configurations["parent_var"] == "parent_value"

    def test_child_declared_args_do_not_leak(self):
        """DeclareLaunchArgument defaults from a child file should NOT
        persist in the parent context (only SetLaunchConfiguration should)."""
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
                        DeclareLaunchArgument("child_only_arg", default_value="should_not_leak"),
                        SetLaunchConfiguration("sticky_var", "persists"),
                    ])
            """,
            )

            ctx = _make_context({})
            R._inline_resolve_python_launch(child_path, ctx, {}, depth=1)

            assert "child_only_arg" not in ctx._launch_configurations
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
            R._inline_resolve_python_launch(child_path, ctx, {"mode": "custom"}, depth=1)

            assert ctx._launch_configurations["resolved_mode"] == "custom"

    def test_depth_limit_prevents_infinite_recursion(self):
        """Exceeding the depth limit should warn, not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = _write_launch_py(
                tmpdir,
                "child.launch.py",
                """\
                from launch import LaunchDescription

                def generate_launch_description():
                    return LaunchDescription([])
            """,
            )

            ctx = _make_context({})
            R._inline_resolve_python_launch(child_path, ctx, {}, depth=21)
            # Should not raise; just warns
            assert any("depth" in w.lower() for w in R._tracked["warnings"])

    def test_missing_file_silently_skipped(self):
        """A non-existent include file should not raise."""
        ctx = _make_context({})
        R._inline_resolve_python_launch("/nonexistent/path.py", ctx, {}, depth=1)
        # No error, no crash

    def test_inline_include_does_not_duplicate_tracked_nodes(self):
        """Inline include must NOT create tracked node entries — the Rust
        orchestrator resolves the child file separately, so any entries
        created by the inline walk would be duplicates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = _write_launch_py(
                tmpdir,
                "child.launch.py",
                """\
                from launch import LaunchDescription
                from launch_ros.actions import Node

                def generate_launch_description():
                    return LaunchDescription([
                        Node(package="my_pkg", executable="my_exec"),
                    ])
            """,
            )

            nodes_before = len(R._tracked["nodes"])
            pkgs_before = list(R._tracked["packages"])
            ctx = _make_context({})
            R._inline_resolve_python_launch(child_path, ctx, {}, depth=1)

            # No new tracked nodes or packages from the inline walk
            assert len(R._tracked["nodes"]) == nodes_before
            assert R._tracked["packages"] == pkgs_before

    def test_inline_include_does_not_duplicate_global_params(self):
        """SetParameter inside an inline-included child must NOT create
        tracked global_params entries — only context mutations survive."""
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

            gp_before = len(R._tracked["global_params"])
            ctx = _make_context({})
            R._inline_resolve_python_launch(child_path, ctx, {}, depth=1)

            # No new tracked global_params
            assert len(R._tracked["global_params"]) == gp_before
            # But context should have the global_params for downstream use
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

            deps_before = len(R._tracked["include_deps"])
            ctx = _make_context({})
            R._inline_resolve_python_launch(child_path, ctx, {}, depth=1)

            # No new include deps
            assert len(R._tracked["include_deps"]) == deps_before


# ─── Environment Variable Stack ──────────────────────────────────────────────


class TestEnvStack:
    """Tests for SetEnvironmentVariable / UnsetEnvironmentVariable tracking,
    env inheritance to nodes, and group scoping."""

    def test_set_env_inherits_to_node(self):
        """SetEnvironmentVariable then Node → node's env includes the var."""
        ctx = _make_context()
        set_env = R._TrackedSetEnvironmentVariable(name="FOO", value="bar")
        node = R._TrackedNode(package="p", executable="e", name="n")
        R._walk_action(set_env, ctx, 0)
        R._walk_action(node, ctx, 0)
        entry = R._tracked["nodes"][node._idx]
        assert entry["env"]["FOO"] == "bar"

    def test_unset_env_nonexistent_errors(self):
        """UnsetEnvironmentVariable on a var that doesn't exist → 'not set' error."""
        import uuid

        name = f"NONEXISTENT_VAR_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        R._walk_action(R._TrackedUnsetEnvironmentVariable(name=name), ctx, 0)
        errors = R._tracked.get("errors", [])
        assert any(name in e and "not set" in e for e in errors)

    def test_unset_env_override_only_accepted(self):
        """UnsetEnv on an override-only var (not in process env) → accepted."""
        import uuid

        name = f"OVERRIDE_ONLY_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        R._walk_action(R._TrackedSetEnvironmentVariable(name=name, value="val"), ctx, 0)
        assert name in R._env
        R._walk_action(R._TrackedUnsetEnvironmentVariable(name=name), ctx, 0)
        assert name not in R._env
        errors = R._tracked.get("errors", [])
        assert not any(name in e for e in errors)

    def test_group_scoped_env_does_not_leak(self):
        """GroupAction(scoped=True) → env mutations don't leak to siblings."""
        ctx = _make_context()
        group = R._TrackedGroupAction(
            actions=[R._TrackedSetEnvironmentVariable(name="SCOPED_VAR", value="val")],
            scoped=True,
        )
        R._walk_action(group, ctx, 0)
        node = R._TrackedNode(package="p", executable="e", name="n")
        R._walk_action(node, ctx, 0)
        entry = R._tracked["nodes"][node._idx]
        assert "SCOPED_VAR" not in entry["env"]

    def test_group_unscoped_env_leaks(self):
        """GroupAction(scoped=False) → env mutations leak to siblings."""
        ctx = _make_context()
        group = R._TrackedGroupAction(
            actions=[R._TrackedSetEnvironmentVariable(name="LEAKED_VAR", value="val")],
            scoped=False,
        )
        R._walk_action(group, ctx, 0)
        node = R._TrackedNode(package="p", executable="e", name="n")
        R._walk_action(node, ctx, 0)
        entry = R._tracked["nodes"][node._idx]
        assert entry["env"]["LEAKED_VAR"] == "val"

    def test_node_local_env_overrides_inherited(self):
        """Node-local env overrides inherited env for the same key."""
        ctx = _make_context()
        R._walk_action(R._TrackedSetEnvironmentVariable(name="FOO", value="inherited"), ctx, 0)
        node = R._TrackedNode(
            package="p",
            executable="e",
            name="n",
            env=[("FOO", "local")],
        )
        R._walk_action(node, ctx, 0)
        entry = R._tracked["nodes"][node._idx]
        assert entry["env"]["FOO"] == "local"

    def test_inline_include_env_rollback(self):
        """Env set by inline-included child does NOT leak to parent."""
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
            R._inline_resolve_python_launch(child_path, ctx, {}, depth=1)
            assert "CHILD_VAR" not in R._env

    def test_env_overrides_returns_only_overrides(self):
        """_env_overrides() returns only explicitly set vars, not process env."""
        R._env["NEW_VAR"] = "new_val"
        overrides = R._env_overrides()
        assert overrides["NEW_VAR"] == "new_val"
        # Process env vars must NOT appear in overrides.
        import os

        assert "PATH" in os.environ, "PATH should exist in process env for this test"
        assert "PATH" not in overrides

    def test_env_overrides_empty_when_no_overrides(self):
        """Empty overrides when nothing has been set."""
        assert R._env_overrides() == {}

    def test_net_zero_error_includes_value(self):
        """Net-zero leak error includes the override value (safe, user-set)."""
        R._env["MY_KEY"] = "my_value"
        # Simulate the net-zero check inline (same logic as main())
        errors = []
        for k, v in R._env.items():
            errors.append(
                f"env var '{k}' was set to '{v}' but not restored (leaked from file scope)"
            )
        err = [e for e in errors if "MY_KEY" in e]
        assert len(err) == 1
        assert "my_value" in err[0]

    def test_process_env_never_exposed_in_node(self):
        """Node env should only contain overrides, never process env vars."""
        ctx = _make_context()
        R._walk_action(R._TrackedSetEnvironmentVariable(name="MY_OVERRIDE", value="val"), ctx, 0)
        node = R._TrackedNode(package="p", executable="e", name="n")
        R._walk_action(node, ctx, 0)
        entry = R._tracked["nodes"][node._idx]
        # Only the explicit override should appear.
        assert entry["env"] == {"MY_OVERRIDE": "val"}
        # Process env vars like PATH must never leak.
        assert "PATH" not in entry["env"]


# ─── XML Parser ──────────────────────────────────────────────────────────────


class TestParseXmlLaunch:
    def test_parse_arg(self):
        xml = '<launch><arg name="x" default="val" description="desc"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert len(elems) == 1
        arg = elems[0]["Arg"]
        assert arg["name"] == "x"
        assert arg["default"] == "val"
        assert arg["description"] == "desc"

    def test_parse_arg_no_default(self):
        xml = '<launch><arg name="x"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0]["Arg"]["default"] is None

    def test_parse_let(self):
        xml = '<launch><let name="v" value="123"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        let = elems[0]["Let"]
        assert let["name"] == "v"
        assert let["value"] == "123"

    def test_parse_let_with_condition(self):
        xml = '<launch><let name="v" value="1" if="$(arg flag)"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        cond = elems[0]["Let"]["condition"]
        assert cond["kind"] == "If"
        assert cond["expr"] == "$(arg flag)"

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
        elems = R.parse_xml_launch(xml, "test.xml")
        node = elems[0]["Node"]
        assert node["pkg"] == "my_pkg"
        assert node["exec"] == "my_exec"
        assert node["name"] == "n"
        assert node["namespace"] == "/ns"
        assert node["output"] == "screen"
        assert len(node["params"]) == 1
        assert node["params"][0]["name"] == "foo"
        assert len(node["remaps"]) == 1
        assert node["remaps"][0]["from"] == "/in"
        assert len(node["envs"]) == 1
        assert node["envs"][0]["name"] == "VAR"

    def test_parse_group_scoped(self):
        xml = textwrap.dedent("""\
            <launch>
                <group scoped="false" if="$(arg x)">
                    <arg name="nested" default="val"/>
                </group>
            </launch>
        """)
        elems = R.parse_xml_launch(xml, "test.xml")
        group = elems[0]["Group"]
        assert group["scoped"] is False
        assert group["condition"]["kind"] == "If"
        assert len(group["children"]) == 1
        assert "Arg" in group["children"][0]

    def test_parse_include_with_args(self):
        xml = textwrap.dedent("""\
            <launch>
                <include file="$(find-pkg-share pkg)/launch/f.xml">
                    <arg name="a" value="1"/>
                    <arg name="b" value="2"/>
                </include>
            </launch>
        """)
        elems = R.parse_xml_launch(xml, "test.xml")
        inc = elems[0]["Include"]
        assert "$(find-pkg-share pkg)" in inc["file"]
        assert len(inc["args"]) == 2
        assert inc["args"][0]["name"] == "a"

    def test_parse_set_env_unset_env(self):
        xml = textwrap.dedent("""\
            <launch>
                <set_env name="X" value="1"/>
                <unset_env name="Y" unless="$(arg flag)"/>
            </launch>
        """)
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0]["SetEnv"]["name"] == "X"
        assert elems[1]["UnsetEnv"]["name"] == "Y"
        assert elems[1]["UnsetEnv"]["condition"]["kind"] == "Unless"

    def test_parse_push_ros_namespace(self):
        xml = '<launch><push-ros-namespace namespace="/my_ns"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0]["PushRosNamespace"]["namespace"] == "/my_ns"

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
        elems = R.parse_xml_launch(xml, "test.xml")
        nc = elems[0]["NodeContainer"]
        assert nc["pkg"] == "rclcpp"
        assert len(nc["composable_nodes"]) == 1
        assert nc["composable_nodes"][0]["plugin"] == "p::N"
        assert len(nc["composable_nodes"][0]["params"]) == 1
        assert len(nc["envs"]) == 1

    def test_parse_load_composable_node(self):
        xml = textwrap.dedent("""\
            <launch>
                <load_composable_node target="container">
                    <composable_node pkg="p" plugin="p::N"/>
                </load_composable_node>
            </launch>
        """)
        elems = R.parse_xml_launch(xml, "test.xml")
        lcn = elems[0]["LoadComposableNode"]
        assert lcn["target"] == "container"
        assert len(lcn["composable_nodes"]) == 1

    def test_parse_set_parameter_set_remap(self):
        xml = textwrap.dedent("""\
            <launch>
                <set_parameter name="p" value="v"/>
                <set_remap from="/a" to="/b"/>
            </launch>
        """)
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0]["SetParameter"]["name"] == "p"
        assert elems[1]["SetRemap"]["from"] == "/a"

    def test_parse_lifecycle_node(self):
        xml = '<launch><lifecycle_node pkg="p" exec="e" name="n"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert "LifecycleNode" in elems[0]
        assert elems[0]["LifecycleNode"]["pkg"] == "p"

    def test_parse_unknown_element(self):
        xml = "<launch><foobar/></launch>"
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0]["UnknownElement"]["tag_name"] == "foobar"

    def test_parse_event_handler(self):
        xml = textwrap.dedent("""\
            <launch>
                <on_process_exit target="my_node">
                    <emit_event event="shutdown"/>
                </on_process_exit>
            </launch>
        """)
        elems = R.parse_xml_launch(xml, "test.xml")
        eh = elems[0]["EventHandler"]
        assert eh["kind"] == "OnProcessExit"
        assert eh["target"] == "my_node"
        assert len(eh["children"]) == 1
        assert eh["children"][0]["EmitEvent"]["event"] == "shutdown"

    def test_parse_param_from(self):
        xml = '<launch><node pkg="p" exec="e"><param from="file.yaml"/></node></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        param = elems[0]["Node"]["params"][0]
        assert param["from"] == "file.yaml"
        assert param["name"] is None

    def test_substitutions_preserved_as_raw_strings(self):
        xml = '<launch><node pkg="$(arg pkg)" exec="$(var exe)"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0]["Node"]["pkg"] == "$(arg pkg)"
        assert elems[0]["Node"]["exec"] == "$(var exe)"


# ─── YAML Parser ─────────────────────────────────────────────────────────────


class TestParseYamlLaunch:
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
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
        assert len(elems) == 2
        assert elems[0]["Arg"]["name"] == "my_arg"
        assert elems[1]["Node"]["pkg"] == "my_pkg"

    def test_parse_yaml_push_ros_namespace(self):
        yaml_content = textwrap.dedent("""\
            launch:
              - push_ros_namespace:
                  namespace: /my_ns
        """)
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
        assert elems[0]["PushRosNamespace"]["namespace"] == "/my_ns"

    def test_parse_yaml_composable_node_container(self):
        yaml_content = textwrap.dedent("""\
            launch:
              - composable_node_container:
                  pkg: rclcpp
                  exec: container
                  name: c
        """)
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
        assert "NodeContainer" in elems[0]

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
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
        group = elems[0]["Group"]
        assert group["scoped"] is False
        assert len(group["children"]) == 1
        assert group["children"][0]["Arg"]["name"] == "nested"

    def test_parse_yaml_missing_launch_key(self):
        yaml_content = "foo: bar"
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
        assert elems == []
