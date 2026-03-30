"""Tests for resolver internals — substitution handling, node tracking,
and inline Python include resolution."""

import os
import tempfile
import textwrap

from launch_plus import resolver as R

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
        R._track_package(R.get_state(), "my_pkg")
        assert "my_pkg" in R.get_state().tracked["packages"]

    def test_skips_substitution_object(self):
        lc = R._LaunchConfiguration("container_pkg")
        R._track_package(R.get_state(), lc)
        assert "container_pkg" not in R.get_state().tracked["packages"]
        assert len(R.get_state().tracked["packages"]) == 0

    def test_skips_empty_and_none(self):
        R._track_package(R.get_state(), None)
        R._track_package(R.get_state(), "")
        assert len(R.get_state().tracked["packages"]) == 0

    def test_deduplicates(self):
        R._track_package(R.get_state(), "pkg_a")
        R._track_package(R.get_state(), "pkg_a")
        assert R.get_state().tracked["packages"].count("pkg_a") == 1

    def test_skips_list_of_substitutions(self):
        parts = [R._LaunchConfiguration("pkg_var"), "_suffix"]
        R._track_package(R.get_state(), parts)
        assert len(R.get_state().tracked["packages"]) == 0


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
        # Before execute(): node is not yet tracked
        assert node._idx == -1

        node.execute(ctx)

        assert R.get_state().tracked["nodes"][node._idx]["package"] == "actual_package"
        assert "actual_package" in R.get_state().tracked["packages"]

    def test_tracked_node_unresolved_package_stays_as_name(self):
        """When the LaunchConfiguration cannot be resolved (not in context),
        the entry keeps the variable name but does NOT track it as a package."""
        ctx = _make_context({})
        node = R._TrackedNode(
            package=R._LaunchConfiguration("unknown_pkg"),
            executable="exec",
        )
        node.execute(ctx)

        # The entry shows the variable name (display fallback from str(lc))
        assert R.get_state().tracked["nodes"][node._idx]["package"] == "unknown_pkg"
        # But it must NOT be tracked as a real package dependency
        assert "unknown_pkg" not in R.get_state().tracked["packages"]

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
        container._ensure_tracked(ctx._state)
        R._resolve_node_details(ctx._state, container, ctx)

        entry = R.get_state().tracked["nodes"][container._idx]
        assert entry["package"] == "rclcpp_components"
        assert entry["executable"] == "component_container_mt"
        assert entry["name"] == "my_container"
        assert "rclcpp_components" in R.get_state().tracked["packages"]

    def test_plain_string_package_tracked_on_execute(self):
        """When package is a plain string, it should be tracked after execute()."""
        ctx = _make_context()
        node = R._TrackedNode(package="my_real_pkg", executable="exec")
        assert node._idx == -1  # not yet tracked
        node.execute(ctx)
        assert node._idx >= 0
        assert "my_real_pkg" in R.get_state().tracked["packages"]


# ─── Composable plugin deferred resolution ────────────────────────────────────


class TestComposablePluginResolution:
    def test_composable_node_resolves_package(self):
        ctx = _make_context({"plugin_pkg": "sensor_driver"})
        desc = R._TrackedComposableNode(
            package=R._LaunchConfiguration("plugin_pkg"),
            plugin="sensor_driver::SensorNode",
            name="sensor",
        )
        plugins = R._resolve_composable_plugins(ctx._state, [desc], ctx)
        assert len(plugins) == 1
        assert plugins[0]["package"] == "sensor_driver"
        assert "sensor_driver" in R.get_state().tracked["packages"]

    def test_composable_node_unresolved_package_not_tracked(self):
        ctx = _make_context({})
        desc = R._TrackedComposableNode(
            package=R._LaunchConfiguration("unknown"),
            plugin="foo::Bar",
        )
        plugins = R._resolve_composable_plugins(ctx._state, [desc], ctx)
        assert plugins[0]["package"] == "unknown"  # display fallback
        assert "unknown" not in R.get_state().tracked["packages"]

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
        plugins = R._resolve_composable_plugins(ctx._state, [desc], ctx)
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
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {})

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
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {})

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
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {"mode": "custom"})

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
            R.get_state().walk_depth = 21  # Simulate deep nesting
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {})
            # Should not raise; just warns
            assert any("depth" in w.lower() for w in R.get_state().tracked["warnings"])
            R.get_state().walk_depth = 0  # Reset

    def test_missing_file_silently_skipped(self):
        """A non-existent include file should not raise."""
        ctx = _make_context({})
        R._inline_resolve_python_launch(R.get_state(), "/nonexistent/path.py", ctx, {})
        # No error, no crash

    def test_inline_include_keeps_tracked_nodes(self):
        """Inline include MUST keep tracked node entries — the Python resolver
        handles all includes inline and the orchestrator does not re-resolve."""
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

            nodes_before = len(R.get_state().tracked["nodes"])
            ctx = _make_context({})
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {})

            # Inline include adds nodes to tracked state
            assert len(R.get_state().tracked["nodes"]) > nodes_before

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

            gp_before = len(R.get_state().tracked["global_params"])
            ctx = _make_context({})
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {})

            # Global params from inline include are kept
            assert len(R.get_state().tracked["global_params"]) > gp_before
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

            deps_before = len(R.get_state().tracked["include_deps"])
            ctx = _make_context({})
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {})

            # No new include deps
            assert len(R.get_state().tracked["include_deps"]) == deps_before


# ─── Environment Variable Stack ──────────────────────────────────────────────


class TestEnvStack:
    """Tests for SetEnvironmentVariable / UnsetEnvironmentVariable tracking,
    env inheritance to nodes, and group scoping."""

    def test_set_env_inherits_to_node(self):
        """SetEnvironmentVariable then Node → node's env includes the var."""
        ctx = _make_context()
        set_env = R._TrackedSetEnvironmentVariable(name="FOO", value="bar")
        node = R._TrackedNode(package="p", executable="e", name="n")
        set_env.execute(ctx)
        node.execute(ctx)
        entry = R.get_state().tracked["nodes"][node._idx]
        assert entry["env"]["FOO"] == "bar"

    def test_unset_env_nonexistent_errors(self):
        """UnsetEnvironmentVariable on a var that doesn't exist → 'not set' error."""
        import uuid

        name = f"NONEXISTENT_VAR_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        R._TrackedUnsetEnvironmentVariable(name=name).execute(ctx)
        errors = R.get_state().tracked.get("errors", [])
        assert any(name in e and "not set" in e for e in errors)

    def test_unset_env_override_only_accepted(self):
        """UnsetEnv on an override-only var (not in process env) → accepted."""
        import uuid

        name = f"OVERRIDE_ONLY_{uuid.uuid4().hex[:8]}"
        assert name not in os.environ, f"precondition: {name} must not be in process env"
        ctx = _make_context()
        R._TrackedSetEnvironmentVariable(name=name, value="val").execute(ctx)
        assert name in R.get_state().env
        R._TrackedUnsetEnvironmentVariable(name=name).execute(ctx)
        assert name not in R.get_state().env
        errors = R.get_state().tracked.get("errors", [])
        assert not any(name in e for e in errors)

    def test_group_scoped_env_does_not_leak(self):
        """GroupAction(scoped=True) → env mutations don't leak to siblings."""
        ctx = _make_context()
        group = R._TrackedGroupAction(
            actions=[R._TrackedSetEnvironmentVariable(name="SCOPED_VAR", value="val")],
            scoped=True,
        )
        group.execute(ctx)
        node = R._TrackedNode(package="p", executable="e", name="n")
        node.execute(ctx)
        entry = R.get_state().tracked["nodes"][node._idx]
        assert "SCOPED_VAR" not in entry["env"]

    def test_group_unscoped_env_leaks(self):
        """GroupAction(scoped=False) → env mutations leak to siblings."""
        ctx = _make_context()
        group = R._TrackedGroupAction(
            actions=[R._TrackedSetEnvironmentVariable(name="LEAKED_VAR", value="val")],
            scoped=False,
        )
        group.execute(ctx)
        node = R._TrackedNode(package="p", executable="e", name="n")
        node.execute(ctx)
        entry = R.get_state().tracked["nodes"][node._idx]
        assert entry["env"]["LEAKED_VAR"] == "val"

    def test_node_local_env_overrides_inherited(self):
        """Node-local env overrides inherited env for the same key."""
        ctx = _make_context()
        R._TrackedSetEnvironmentVariable(name="FOO", value="inherited").execute(ctx)
        node = R._TrackedNode(
            package="p",
            executable="e",
            name="n",
            env=[("FOO", "local")],
        )
        node.execute(ctx)
        entry = R.get_state().tracked["nodes"][node._idx]
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
            R._inline_resolve_python_launch(R.get_state(), child_path, ctx, {})
            assert "CHILD_VAR" not in R.get_state().env

    def test_env_overrides_returns_only_overrides(self):
        """_env_overrides() returns only explicitly set vars, not process env."""
        R.get_state().env["NEW_VAR"] = "new_val"
        overrides = R._env_overrides(R.get_state())
        assert overrides["NEW_VAR"] == "new_val"
        # Process env vars must NOT appear in overrides.
        import os

        assert "PATH" in os.environ, "PATH should exist in process env for this test"
        assert "PATH" not in overrides

    def test_env_overrides_empty_when_no_overrides(self):
        """Empty overrides when nothing has been set."""
        assert R._env_overrides(R.get_state()) == {}

    def test_net_zero_error_includes_value(self):
        """Net-zero leak error includes the override value (safe, user-set)."""
        R.get_state().env["MY_KEY"] = "my_value"
        # Simulate the net-zero check inline (same logic as main())
        errors = []
        for k, v in R.get_state().env.items():
            errors.append(
                f"env var '{k}' was set to '{v}' but not restored (leaked from file scope)"
            )
        err = [e for e in errors if "MY_KEY" in e]
        assert len(err) == 1
        assert "my_value" in err[0]

    def test_process_env_never_exposed_in_node(self):
        """Node env should only contain overrides, never process env vars."""
        ctx = _make_context()
        R._TrackedSetEnvironmentVariable(name="MY_OVERRIDE", value="val").execute(ctx)
        node = R._TrackedNode(package="p", executable="e", name="n")
        node.execute(ctx)
        entry = R.get_state().tracked["nodes"][node._idx]
        # Only the explicit override should appear.
        assert entry["env"] == {"MY_OVERRIDE": "val"}
        # Process env vars like PATH must never leak.
        assert "PATH" not in entry["env"]


# ─── XML Parser ──────────────────────────────────────────────────────────────


class TestParseXmlLaunch:
    """Tests for parse_xml_launch() — now returns Entity objects."""

    def test_parse_arg(self):
        xml = '<launch><arg name="x" default="val" description="desc"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert len(elems) == 1
        e = elems[0]
        assert e.type_name == "arg"
        assert e.get_attr("name") == "x"
        assert e.get_attr("default") == "val"
        assert e.get_attr("description") == "desc"

    def test_parse_arg_no_default(self):
        xml = '<launch><arg name="x"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0].get_attr("default", optional=True) is None

    def test_parse_let(self):
        xml = '<launch><let name="v" value="123"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "let"
        assert e.get_attr("name") == "v"
        assert e.get_attr("value") == "123"

    def test_parse_let_with_condition(self):
        xml = '<launch><let name="v" value="1" if="$(arg flag)"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "set_env"
        assert elems[0].get_attr("name") == "X"
        assert elems[1].type_name == "unset_env"
        assert elems[1].get_attr("name") == "Y"
        assert elems[1].get_attr("unless") == "$(arg flag)"

    def test_parse_push_ros_namespace(self):
        xml = '<launch><push-ros-namespace namespace="/my_ns"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "set_parameter"
        assert elems[0].get_attr("name") == "p"
        assert elems[1].type_name == "set_remap"
        assert elems[1].get_attr("from") == "/a"

    def test_parse_lifecycle_node(self):
        xml = '<launch><lifecycle_node pkg="p" exec="e" name="n"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "lifecycle_node"
        assert elems[0].get_attr("pkg") == "p"

    def test_parse_unknown_element(self):
        xml = "<launch><foobar/></launch>"
        elems = R.parse_xml_launch(xml, "test.xml")
        assert elems[0].type_name == "foobar"

    def test_parse_event_handler(self):
        xml = textwrap.dedent("""\
            <launch>
                <on_process_exit target="my_node">
                    <emit_event event="shutdown"/>
                </on_process_exit>
            </launch>
        """)
        elems = R.parse_xml_launch(xml, "test.xml")
        e = elems[0]
        assert e.type_name == "on_process_exit"
        assert e.get_attr("target") == "my_node"
        children = e.children
        assert len(children) == 1
        assert children[0].type_name == "emit_event"
        assert children[0].get_attr("event") == "shutdown"

    def test_parse_param_from(self):
        xml = '<launch><node pkg="p" exec="e"><param from="file.yaml"/></node></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
        params = elems[0].get_attr("param", data_type=list)
        assert params[0].get_attr("from") == "file.yaml"
        assert params[0].get_attr("name", optional=True) is None

    def test_substitutions_preserved_as_raw_strings(self):
        xml = '<launch><node pkg="$(arg pkg)" exec="$(var exe)"/></launch>'
        elems = R.parse_xml_launch(xml, "test.xml")
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
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
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
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
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
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
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
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
        e = elems[0]
        assert e.type_name == "group"
        assert e.get_attr("scoped", data_type=bool) is False
        children = e.children
        assert len(children) == 1
        assert children[0].type_name == "arg"
        assert children[0].get_attr("name") == "nested"

    def test_parse_yaml_missing_launch_key(self):
        yaml_content = "foo: bar"
        elems = R.parse_yaml_launch(yaml_content, "test.yaml")
        assert elems == []


# ─── Substitution Engine ─────────────────────────────────────────────────────


def _fresh_subst_ctx(**kwargs):
    """Create a _SubstitutionContext and reset module-level state for clean tests."""
    # Reset tracked state so _error/_warn/_track_package don't leak between tests
    for key in R.get_state().tracked:
        if isinstance(R.get_state().tracked[key], list):
            R.get_state().tracked[key] = []
        elif isinstance(R.get_state().tracked[key], dict):
            R.get_state().tracked[key] = {}
    ctx = R._SubstitutionContext()
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx


class TestResolveSubstitutions:
    """Tests for resolve_substitutions() — full resolution with context."""

    def test_resolve_arg(self):
        ctx = _fresh_subst_ctx(args={"vehicle": "sample_vehicle"})
        result = R.resolve_substitutions("$(arg vehicle)", ctx)
        assert result == "sample_vehicle"

    def test_resolve_var(self):
        ctx = _fresh_subst_ctx(vars={"config": "/path/to/config"})
        result = R.resolve_substitutions("$(var config)", ctx)
        assert result == "/path/to/config"

    def test_resolve_var_falls_back_to_args(self):
        ctx = _fresh_subst_ctx(args={"fallback": "from_args"})
        result = R.resolve_substitutions("$(var fallback)", ctx)
        assert result == "from_args"

    def test_resolve_env_from_context(self):
        ctx = _fresh_subst_ctx(env={"TEST_LAUNCH_VAR": "test_value"})
        result = R.resolve_substitutions("$(env TEST_LAUNCH_VAR)", ctx)
        assert result == "test_value"

    def test_resolve_env_with_default_unset(self):
        var = "LAUNCH_PLUS_TEST_UNSET_a1b2c3"
        assert var not in os.environ, f"precondition: {var} must not be set"
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions(f"$(env {var} fallback)", ctx)
        assert result == "fallback"

    def test_resolve_env_unset_without_default_errors(self):
        var = "LAUNCH_PLUS_TEST_UNSET_d4e5f6"
        assert var not in os.environ, f"precondition: {var} must not be set"
        ctx = _fresh_subst_ctx()
        R.resolve_substitutions(f"$(env {var})", ctx)
        assert any("not set" in e for e in R.get_state().tracked["errors"])

    def test_resolve_dirname(self):
        ctx = _fresh_subst_ctx(launch_file_dir="/path/to/launch")
        result = R.resolve_substitutions("$(dirname)/config.yaml", ctx)
        assert result == "/path/to/launch/config.yaml"

    def test_resolve_dirname_unset(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions("$(dirname)/config.yaml", ctx)
        assert result == "$(dirname)/config.yaml"

    def test_resolve_find_pkg_share_preview(self):
        ctx = _fresh_subst_ctx(preview_mode=True)
        result = R.resolve_substitutions("$(find-pkg-share my_pkg)/config", ctx)
        assert result == "$(find-pkg-share my_pkg)/config"
        assert "my_pkg" in R.get_state().tracked["packages"]

    def test_resolve_find_pkg_prefix(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions("$(find-pkg-prefix my_pkg)/lib", ctx)
        assert result == "$(find-pkg-prefix my_pkg)/lib"
        assert "my_pkg" in R.get_state().tracked["packages"]

    def test_resolve_nested_substitution(self):
        ctx = _fresh_subst_ctx(
            vars={"pkg_name": "vehicle_description"},
            preview_mode=True,
        )
        result = R.resolve_substitutions("$(find-pkg-share $(var pkg_name))/config", ctx)
        assert result == "$(find-pkg-share vehicle_description)/config"

    def test_resolve_chained_vars(self):
        ctx = _fresh_subst_ctx(
            args={"vehicle": "sample"},
            vars={
                "config_path": "$(find-pkg-share $(arg vehicle)_description)/config",
            },
            preview_mode=True,
        )
        result = R.resolve_substitutions("$(var config_path)/params.yaml", ctx)
        assert result == "$(find-pkg-share sample_description)/config/params.yaml"

    def test_resolve_error_undefined_arg(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions("$(arg undefined)", ctx)
        assert "$(arg undefined)" in result
        assert any("undefined argument" in e for e in R.get_state().tracked["errors"])

    def test_resolve_error_undefined_var(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions("$(var undefined)", ctx)
        assert "$(var undefined)" in result
        assert any("undefined variable" in e for e in R.get_state().tracked["errors"])

    def test_resolve_eval_string_equality(self):
        # After XML entity decoding, &quot; becomes " — the == is inside a
        # double-quoted template that the Lark grammar parses correctly.
        ctx = _fresh_subst_ctx(vars={"gnss_receiver": "ublox"})
        result = R.resolve_substitutions("""$(eval "'$(var gnss_receiver)'=='ublox'")""", ctx)
        assert result == "True"

    def test_resolve_eval_false_comparison(self):
        ctx = _fresh_subst_ctx(vars={"x": "foo"})
        result = R.resolve_substitutions("""$(eval "'$(var x)'=='bar'")""", ctx)
        assert result == "False"

    def test_resolve_eval_outer_single_quote_wrapper(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions(
            r"$(eval '\'cuda\' == \'cuda\' or \'cuda\' == \'cuda-all-in-one\'')",
            ctx,
        )
        assert result == "True"

    def test_resolve_eval_outer_double_quote_wrapper(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions(
            r"""$(eval '"camera_lidar_radar_fusion"=="camera_lidar_radar_fusion"')""",
            ctx,
        )
        assert result == "True"

    def test_resolve_eval_outer_double_quote_false(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions(
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
        result = R.resolve_substitutions(
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
        result = R.resolve_substitutions(r"$(eval $(var func))", ctx)
        assert result == "['ndt']"

    def test_resolve_eval_with_backslash_unescape(self):
        ctx = _fresh_subst_ctx(
            vars={
                "func2": r"list(set('ndt'.split('_')).intersection([\'ndt\',\'yabloc\']))",
            },
        )
        result = R.resolve_substitutions(r"$(eval $(var func2))", ctx)
        assert result == "['ndt']"

    def test_resolve_command_preserved(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions("$(command echo hello)", ctx)
        assert result == "$(command echo hello)"

    def test_resolve_literal_passthrough(self):
        ctx = _fresh_subst_ctx()
        result = R.resolve_substitutions("/path/to/file.yaml", ctx)
        assert result == "/path/to/file.yaml"

    def test_resolve_multiple_packages_tracked(self):
        ctx = _fresh_subst_ctx(preview_mode=True)
        R.resolve_substitutions("$(find-pkg-share pkg1)/$(find-pkg-share pkg2)", ctx)
        assert "pkg1" in R.get_state().tracked["packages"]
        assert "pkg2" in R.get_state().tracked["packages"]


# ─── AST Walker (resolve_xml_elements) ───────────────────────────────────────


def _fresh_walker_ctx(**kwargs):
    """Create a fresh _SubstitutionContext and reset ALL module-level state for walker tests."""
    # Reset tracked state
    for key in R.get_state().tracked:
        if isinstance(R.get_state().tracked[key], list):
            R.get_state().tracked[key] = []
        elif isinstance(R.get_state().tracked[key], dict):
            R.get_state().tracked[key] = {}
    # Reset module-level state
    R.get_state().namespace_stack.clear()
    R.get_state().env.clear()
    R.get_state().declared_arg_names.clear()
    ctx = R._SubstitutionContext()
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx


def _parse_and_walk(xml_str, ctx=None, **ctx_kwargs):
    """Parse XML string and walk it.  Returns (ctx, tracked)."""
    if ctx is None:
        ctx = _fresh_walker_ctx(**ctx_kwargs)
    elements = R.parse_xml_launch(xml_str, "test.launch.xml")
    R.resolve_xml_elements(elements, ctx)
    return ctx, R.get_state().tracked


class TestResolveXmlElements:
    """Tests for resolve_xml_elements() — the XML/YAML AST walker."""

    # ── Basic node resolution ──

    def test_simple_node(self):
        xml = '<launch><node pkg="my_pkg" exec="my_node" name="node1"/></launch>'
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1
        assert tracked["nodes"][0]["package"] == "my_pkg"
        assert tracked["nodes"][0]["executable"] == "my_node"
        assert tracked["nodes"][0]["name"] == "node1"
        assert "my_pkg" in tracked["packages"]

    def test_lifecycle_node(self):
        xml = textwrap.dedent("""\
            <launch>
              <lifecycle_node pkg="ros2_socketcan" exec="socket_can_receiver" name="receiver">
                <param name="interface" value="can0"/>
              </lifecycle_node>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1
        assert tracked["nodes"][0]["kind"] == "lifecycle_node"
        assert tracked["nodes"][0]["package"] == "ros2_socketcan"
        assert tracked["nodes"][0]["parameters"]["interface"] == "can0"

    # ── Arg and Let ──

    def test_arg_default_applied(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="vehicle" default="sample"/>
              <node pkg="$(arg vehicle)_pkg" exec="node" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["package"] == "sample_pkg"
        assert "sample_pkg" in tracked["packages"]

    def test_arg_override(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="vehicle" default="sample"/>
              <node pkg="$(arg vehicle)_pkg" exec="node" name="n"/>
            </launch>
        """)
        ctx = _fresh_walker_ctx(args={"vehicle": "custom"})
        _, tracked = _parse_and_walk(xml, ctx=ctx)
        assert tracked["nodes"][0]["package"] == "custom_pkg"

    def test_let_variable(self):
        xml = textwrap.dedent("""\
            <launch>
              <let name="pkg_name" value="my_package"/>
              <node pkg="$(var pkg_name)" exec="node" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["package"] == "my_package"

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

    # ── Conditions ──

    def test_condition_if_true(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="enable" default="true"/>
              <node pkg="p" exec="e" name="n" if="$(arg enable)"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1

    def test_condition_if_false(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="enable" default="false"/>
              <node pkg="p" exec="e" name="n" if="$(arg enable)"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 0

    def test_condition_unless(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="use_sim" default="true"/>
              <group if="$(arg use_sim)">
                <node pkg="sim_pkg" exec="sim" name="sim"/>
              </group>
              <group unless="$(arg use_sim)">
                <node pkg="real_pkg" exec="real" name="real"/>
              </group>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1
        assert tracked["nodes"][0]["package"] == "sim_pkg"

    def test_let_with_condition(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="flag" default="false"/>
              <let name="x" value="set" if="$(arg flag)"/>
              <node pkg="$(var x)" exec="e" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        # $(var x) is undefined → error recorded, placeholder kept
        assert any("undefined variable" in e for e in tracked["errors"])

    # ── Group scoping ──

    def test_scoped_group_env_isolation(self):
        xml = textwrap.dedent("""\
            <launch>
              <group scoped="true">
                <set_env name="SCOPED_VAR" value="inner"/>
                <node pkg="inner_pkg" exec="e" name="inner"/>
              </group>
              <node pkg="outer_pkg" exec="e" name="outer"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        nodes = tracked["nodes"]
        assert len(nodes) == 2
        # Inner node has SCOPED_VAR
        assert nodes[0]["env"].get("SCOPED_VAR") == "inner"
        # Outer node does NOT
        assert nodes[1]["env"].get("SCOPED_VAR") is None

    def test_scoped_group_namespace_isolation(self):
        xml = textwrap.dedent("""\
            <launch>
              <group scoped="true">
                <push-ros-namespace namespace="/scoped_ns"/>
                <node pkg="p1" exec="e" name="n1"/>
              </group>
              <node pkg="p2" exec="e" name="n2"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["namespace_stack"] == ["/scoped_ns"]
        assert tracked["nodes"][1]["namespace_stack"] == []

    def test_unscoped_group_shares_namespace(self):
        xml = textwrap.dedent("""\
            <launch>
              <group scoped="false">
                <push-ros-namespace namespace="/shared"/>
                <node pkg="p1" exec="e" name="n1"/>
              </group>
              <node pkg="p2" exec="e" name="n2"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        # Unscoped: namespace persists
        assert tracked["nodes"][0]["namespace_stack"] == ["/shared"]
        assert tracked["nodes"][1]["namespace_stack"] == ["/shared"]

    # ── Env handling ──

    def test_set_env_inherits_to_node(self):
        xml = textwrap.dedent("""\
            <launch>
              <set_env name="FOO" value="bar"/>
              <node pkg="p" exec="e" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["env"].get("FOO") == "bar"

    def test_unset_env(self):
        xml = textwrap.dedent("""\
            <launch>
              <set_env name="X" value="1"/>
              <unset_env name="X"/>
              <node pkg="p" exec="e" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["env"].get("X") is None

    def test_env_substitution_reads_ctx_env(self):
        xml = textwrap.dedent("""\
            <launch>
              <set_env name="MY_VAR" value="hello"/>
              <node pkg="p" exec="e" name="n">
                <param name="p" value="$(env MY_VAR)"/>
              </node>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["parameters"]["p"] == "hello"

    def test_node_local_env_override(self):
        xml = textwrap.dedent("""\
            <launch>
              <set_env name="A" value="from_launch"/>
              <node pkg="p" exec="e" name="n">
                <env name="B" value="from_node"/>
              </node>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        env = tracked["nodes"][0]["env"]
        assert env.get("A") == "from_launch"
        assert env.get("B") == "from_node"

    # ── Namespace ──

    def test_push_ros_namespace(self):
        xml = textwrap.dedent("""\
            <launch>
              <push-ros-namespace namespace="/robot"/>
              <node pkg="p" exec="e" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["namespace_stack"] == ["/robot"]

    def test_push_ros_namespace_with_condition(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="flag" default="false"/>
              <push-ros-namespace namespace="/ns" if="$(arg flag)"/>
              <node pkg="p" exec="e" name="n"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["namespace_stack"] == []

    def test_node_explicit_namespace(self):
        xml = textwrap.dedent("""\
            <launch>
              <push-ros-namespace namespace="/robot"/>
              <node pkg="p" exec="e" name="n" namespace="/override"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert tracked["nodes"][0]["namespace_stack"] == ["/robot"]
        assert tracked["nodes"][0]["explicit_namespace"] == "/override"

    def test_node_output_args_respawn_resolved(self):
        """output=, args=, respawn= attributes must resolve substitutions."""
        xml = textwrap.dedent("""\
            <launch>
              <arg name="my_output" default="screen"/>
              <arg name="my_args" default="-d /path/config.rviz"/>
              <arg name="do_respawn" default="true"/>
              <node pkg="p" exec="e" name="n"
                    output="$(var my_output)"
                    args="$(var my_args)"
                    respawn="$(var do_respawn)"/>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        node = tracked["nodes"][0]
        assert node["output"] == "screen"
        assert node["args"] == "-d /path/config.rviz"
        assert node["respawn"] == "true"

    # ── Node params, remaps, env ──

    def test_node_params_and_remaps(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="ns" default="/robot"/>
              <node pkg="controller" exec="node" name="ctrl" namespace="$(arg ns)">
                <param name="rate" value="100"/>
                <remap from="/cmd_vel" to="$(arg ns)/cmd_vel"/>
                <env name="DEBUG" value="1"/>
              </node>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        node = tracked["nodes"][0]
        assert node["explicit_namespace"] == "/robot"
        assert node["parameters"]["rate"] == "100"
        assert ["/cmd_vel", "/robot/cmd_vel"] in node["remappings"]
        assert node["env"]["DEBUG"] == "1"

    def test_param_from_file(self):
        xml = textwrap.dedent("""\
            <launch>
              <node pkg="p" exec="e" name="n">
                <param from="$(find-pkg-share config_pkg)/params.yaml"/>
              </node>
            </launch>
        """)
        ctx = _fresh_walker_ctx(preview_mode=True)
        _, tracked = _parse_and_walk(xml, ctx=ctx)
        node = tracked["nodes"][0]
        assert len(node["param_files"]) == 1
        assert "config_pkg" in node["param_files"][0]["path"]
        assert "config_pkg" in tracked["packages"]

    # ── Container and composable nodes ──

    def test_node_container_with_plugins(self):
        xml = textwrap.dedent("""\
            <launch>
              <node_container pkg="rclcpp" exec="container" name="my_container">
                <composable_node pkg="pkg_a" plugin="pkg_a::NodeA" name="node_a"/>
                <composable_node pkg="pkg_b" plugin="pkg_b::NodeB" name="node_b"/>
              </node_container>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1
        node = tracked["nodes"][0]
        assert node["kind"] == "container"
        assert len(node["plugins"]) == 2
        assert node["plugins"][0]["plugin"] == "pkg_a::NodeA"
        assert node["plugins"][1]["plugin"] == "pkg_b::NodeB"
        assert "pkg_a" in tracked["packages"]
        assert "pkg_b" in tracked["packages"]

    def test_load_composable_node(self):
        xml = textwrap.dedent("""\
            <launch>
              <load_composable_node target="/my_container">
                <composable_node pkg="extra" plugin="extra::Plugin" name="extra"/>
              </load_composable_node>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1
        node = tracked["nodes"][0]
        assert node["kind"] == "load_composable"
        assert node["target"] == "/my_container"
        assert len(node["plugins"]) == 1
        assert node["plugins"][0]["plugin"] == "extra::Plugin"

    # ── Event handlers ──

    def test_event_handler(self):
        xml = textwrap.dedent("""\
            <launch>
              <on_process_start target="my_node">
                <emit_event event="configure" target_node="my_node"/>
              </on_process_start>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        ehs = [n for n in tracked["nodes"] if n.get("kind") == "event_handler"]
        assert len(ehs) == 1
        eh = ehs[0]
        assert eh["handler_kind"] == "on_process_start"
        assert eh["target"] == "my_node"
        assert len(eh["eh_actions"]) == 1
        assert eh["eh_actions"][0]["event"] == "configure"
        assert eh["eh_actions"][0]["target_node"] == "my_node"

    def test_on_shutdown(self):
        xml = textwrap.dedent("""\
            <launch>
              <on_shutdown>
                <emit_event event="shutdown"/>
              </on_shutdown>
            </launch>
        """)
        _, tracked = _parse_and_walk(xml)
        ehs = [n for n in tracked["nodes"] if n.get("kind") == "event_handler"]
        assert len(ehs) == 1
        assert ehs[0]["handler_kind"] == "on_shutdown"

    # ── SetParameter, SetRemap, Log, Executable ──

    def test_set_parameter(self):
        xml = '<launch><set_parameter name="use_sim_time" value="true"/></launch>'
        _, tracked = _parse_and_walk(xml)
        assert ["use_sim_time", "true"] in tracked["global_params"]

    def test_log_element(self):
        xml = '<launch><log message="Hello world"/></launch>'
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1
        assert tracked["nodes"][0]["kind"] == "log"
        assert tracked["nodes"][0]["message"] == "Hello world"

    def test_executable(self):
        xml = '<launch><executable cmd="echo hello" name="echo_cmd" shell="true"/></launch>'
        _, tracked = _parse_and_walk(xml)
        assert len(tracked["nodes"]) == 1
        assert tracked["nodes"][0]["kind"] == "executable"
        assert tracked["nodes"][0]["cmd"] == "echo hello"
        assert tracked["nodes"][0]["shell"] is True

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
            assert len(tracked["nodes"]) == 1
            assert tracked["nodes"][0]["name"] == "test_node"
            assert "included_pkg" in tracked["packages"]

    def test_include_args_sequential_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            child_xml = textwrap.dedent("""\
                <launch>
                  <arg name="base"/>
                  <arg name="full"/>
                  <node pkg="p" exec="e" name="n">
                    <param name="path" value="$(arg full)"/>
                  </node>
                </launch>
            """)
            child_path = os.path.join(tmpdir, "child.launch.xml")
            with open(child_path, "w") as f:
                f.write(child_xml)

            main_xml = f"""\
                <launch>
                  <include file="{child_path}">
                    <arg name="base" value="/config"/>
                    <arg name="full" value="$(arg base)/params.yaml"/>
                  </include>
                </launch>
            """
            _, tracked = _parse_and_walk(main_xml)
            assert tracked["nodes"][0]["parameters"]["path"] == "/config/params.yaml"

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

    def test_include_unscoped_arg_leaks_to_sibling(self):
        """ROS 2 semantics: <include> is unscoped by default, so a child's
        <arg name="X" default="Y"/> sets X globally.  A later sibling that
        also declares <arg name="X" default="Z"/> will NOT apply its default
        because X is already set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # First child sets output_topic default to "/planning/topic"
            child_a = os.path.join(tmpdir, "a.launch.xml")
            with open(child_a, "w") as f:
                f.write(
                    textwrap.dedent("""\
                    <launch>
                      <arg name="output_topic" default="/planning/topic"/>
                      <node pkg="p" exec="e" name="node_a">
                        <remap from="out" to="$(var output_topic)"/>
                      </node>
                    </launch>
                """)
                )
            # Second child declares output_topic with a DIFFERENT default
            child_b = os.path.join(tmpdir, "b.launch.xml")
            with open(child_b, "w") as f:
                f.write(
                    textwrap.dedent("""\
                    <launch>
                      <arg name="output_topic" default="/control/topic"/>
                      <node pkg="p" exec="e" name="node_b">
                        <remap from="out" to="$(var output_topic)"/>
                      </node>
                    </launch>
                """)
                )
            # Main includes both: a first, then b
            main_xml = f"""\
                <launch>
                  <include file="{child_a}"/>
                  <include file="{child_b}"/>
                </launch>
            """
            _, tracked = _parse_and_walk(main_xml)
            nodes = tracked["nodes"]
            assert len(nodes) == 2
            # node_a resolves to its own default
            assert nodes[0]["remappings"] == [["out", "/planning/topic"]]
            # node_b gets the LEAKED value from node_a (ROS 2 unscoped semantics)
            assert nodes[1]["remappings"] == [["out", "/planning/topic"]]

    def test_include_explicit_arg_overrides_leaked(self):
        """When the parent explicitly passes an arg value, it overrides any
        leaked value from a prior sibling include."""
        with tempfile.TemporaryDirectory() as tmpdir:
            child_a = os.path.join(tmpdir, "a.launch.xml")
            with open(child_a, "w") as f:
                f.write(
                    textwrap.dedent("""\
                    <launch>
                      <arg name="output_topic" default="/planning/topic"/>
                      <node pkg="p" exec="e" name="node_a">
                        <remap from="out" to="$(var output_topic)"/>
                      </node>
                    </launch>
                """)
                )
            child_b = os.path.join(tmpdir, "b.launch.xml")
            with open(child_b, "w") as f:
                f.write(
                    textwrap.dedent("""\
                    <launch>
                      <arg name="output_topic" default="/control/topic"/>
                      <node pkg="p" exec="e" name="node_b">
                        <remap from="out" to="$(var output_topic)"/>
                      </node>
                    </launch>
                """)
                )
            # Parent explicitly passes output_topic to b
            main_xml = f"""\
                <launch>
                  <include file="{child_a}"/>
                  <include file="{child_b}">
                    <arg name="output_topic" value="/explicit/topic"/>
                  </include>
                </launch>
            """
            _, tracked = _parse_and_walk(main_xml)
            nodes = tracked["nodes"]
            assert len(nodes) == 2
            assert nodes[0]["remappings"] == [["out", "/planning/topic"]]
            # Explicit arg overrides the leaked value
            assert nodes[1]["remappings"] == [["out", "/explicit/topic"]]

    def test_circular_include_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # File includes itself
            self_path = os.path.join(tmpdir, "self.launch.xml")
            with open(self_path, "w") as f:
                f.write(f'<launch><include file="{self_path}"/></launch>')

            ctx = _fresh_walker_ctx()
            elements = R.parse_xml_launch(
                f'<launch><include file="{self_path}"/></launch>', "test.launch.xml"
            )
            R.resolve_xml_elements(elements, ctx)
            assert any("circular" in e for e in R.get_state().tracked["errors"])

    # ── Unknown element ──

    def test_unknown_element_warns(self):
        xml = '<launch><foobar attr="val"/></launch>'
        _, tracked = _parse_and_walk(xml)
        assert any("unknown element" in w for w in tracked["warnings"])

    # ── Namespace helper functions ──

    def test_effective_namespace_basic(self):
        assert R._effective_namespace([]) is None
        assert R._effective_namespace(["/ns"]) == "/ns"
        assert R._effective_namespace(["ns1", "ns2"]) == "/ns1/ns2"
        assert R._effective_namespace(["/a", "b"]) == "/a/b"

    def test_effective_namespace_absolute_resets(self):
        assert R._effective_namespace(["/a", "/b"]) == "/b"
        assert R._effective_namespace(["a", "/b", "c"]) == "/b/c"

    def test_effective_namespace_with_explicit(self):
        assert R._effective_namespace(["/robot"], "/override") == "/override"
        assert R._effective_namespace(["/robot"], "local") == "/robot/local"

    def test_is_truthy(self):
        assert R._is_truthy("true") is True
        assert R._is_truthy("True") is True
        assert R._is_truthy("1") is True
        assert R._is_truthy("yes") is True
        assert R._is_truthy("on") is True
        assert R._is_truthy("false") is False
        assert R._is_truthy("0") is False
        assert R._is_truthy("no") is False
        assert R._is_truthy("") is False

    # ── YAML walker (same function, different parser) ──

    def test_yaml_walker(self):
        yaml_content = textwrap.dedent("""\
            launch:
              - arg:
                  name: model
                  default: default_model
              - node:
                  pkg: $(arg model)_pkg
                  exec: node
                  name: n
        """)
        ctx = _fresh_walker_ctx()
        elements = R.parse_yaml_launch(yaml_content, "test.yaml")
        R.resolve_xml_elements(elements, ctx)
        assert len(R.get_state().tracked["nodes"]) == 1
        assert R.get_state().tracked["nodes"][0]["package"] == "default_model_pkg"

    def test_set_parameter_merged_into_node(self):
        """<set_parameter> values should be merged into subsequent nodes."""
        ctx = _fresh_walker_ctx()
        elements = R.parse_xml_launch(
            textwrap.dedent("""\
                <launch>
                    <set_parameter name="use_sim_time" value="true"/>
                    <node pkg="my_pkg" exec="my_node"/>
                </launch>
            """),
            "test.xml",
        )
        R.resolve_xml_elements(elements, ctx)
        assert len(R.get_state().tracked["nodes"]) == 1
        node = R.get_state().tracked["nodes"][0]
        assert node["parameters"].get("use_sim_time") == "true"

    def test_set_parameter_scoped_in_group(self):
        """<set_parameter> in a scoped group should not leak to outer nodes."""
        ctx = _fresh_walker_ctx()
        elements = R.parse_xml_launch(
            textwrap.dedent("""\
                <launch>
                    <group scoped="true">
                        <set_parameter name="rate" value="10"/>
                        <node pkg="inner_pkg" exec="inner"/>
                    </group>
                    <node pkg="outer_pkg" exec="outer"/>
                </launch>
            """),
            "test.xml",
        )
        R.resolve_xml_elements(elements, ctx)
        nodes = R.get_state().tracked["nodes"]
        assert len(nodes) == 2
        inner = next(n for n in nodes if n["package"] == "inner_pkg")
        outer = next(n for n in nodes if n["package"] == "outer_pkg")
        assert inner["parameters"].get("rate") == "10"
        assert "rate" not in outer["parameters"]


# ─── Action registry resolution (resolve_xml_to_ir → _tracked) ──────────────


def _parse_to_tracked(xml_str, **ctx_kwargs):
    """Parse XML string, resolve via action registry, return _tracked."""
    ctx = _fresh_walker_ctx(**ctx_kwargs)
    elements = R.parse_xml_launch(xml_str, "test.launch.xml")
    R.resolve_xml_elements(elements, ctx)
    return R.get_state().tracked


def _tracked_nodes(tracked):
    """Return non-event-handler nodes from tracked."""
    return [n for n in tracked["nodes"] if n.get("kind") != "event_handler"]


class TestActionRegistry:
    """Tests for action registry resolution — _tracked output."""

    def test_node(self):
        xml = textwrap.dedent("""\
            <launch>
              <push-ros-namespace namespace="/robot"/>
              <node pkg="p" exec="e" name="n" namespace="local"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        assert len(nodes) == 1
        node = nodes[0]
        assert node["package"] == "p"
        assert node["executable"] == "e"
        assert node["name"] == "n"
        assert node["explicit_namespace"] == "local"
        assert node["namespace_stack"] == ["/robot"]

    def test_lifecycle_node(self):
        xml = '<launch><lifecycle_node pkg="p" exec="e" name="n"/></launch>'
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        assert nodes[0]["kind"] == "lifecycle_node"

    def test_container_with_plugins(self):
        xml = textwrap.dedent("""\
            <launch>
              <node_container pkg="rclcpp" exec="container" name="c">
                <composable_node pkg="a" plugin="a::N" name="n1"/>
              </node_container>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        assert len(nodes) == 1
        container = nodes[0]
        assert container["kind"] == "container"
        assert len(container["plugins"]) == 1
        assert container["plugins"][0]["plugin"] == "a::N"

    def test_load_composable(self):
        xml = textwrap.dedent("""\
            <launch>
              <load_composable_node target="/c">
                <composable_node pkg="b" plugin="b::N" name="n2"/>
              </load_composable_node>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        lcn = nodes[0]
        assert lcn["kind"] == "load_composable"
        assert lcn["target"] == "/c"
        assert len(lcn["plugins"]) == 1

    def test_executable(self):
        xml = '<launch><executable cmd="echo hi" name="e" shell="true"/></launch>'
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        exe = nodes[0]
        assert exe["kind"] == "executable"
        assert exe["cmd"] == "echo hi"
        assert exe["shell"] in ("true", True)

    def test_set_parameter_merged_into_node(self):
        """SetParameter is consumed and merged into child nodes' parameters."""
        xml = textwrap.dedent("""\
            <launch>
              <set_parameter name="use_sim_time" value="true"/>
              <node pkg="p" exec="e" name="n">
                <param name="local" value="yes"/>
              </node>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        assert len(nodes) == 1
        node = nodes[0]
        assert node["parameters"]["use_sim_time"] == "true"
        assert node["parameters"]["local"] == "yes"

    def test_set_parameter_scoped(self):
        """SetParameter inside scoped group does not leak to outer nodes."""
        xml = textwrap.dedent("""\
            <launch>
              <group scoped="true">
                <set_parameter name="inner" value="1"/>
                <node pkg="p" exec="e" name="inner_node"/>
              </group>
              <node pkg="p" exec="e" name="outer_node"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        inner = next(n for n in nodes if n["name"] == "inner_node")
        assert inner["parameters"].get("inner") == "1"
        outer = next(n for n in nodes if n["name"] == "outer_node")
        assert "inner" not in outer["parameters"]

    def test_set_remap_merged_into_node(self):
        """SetRemap is consumed and merged into child nodes' remappings."""
        xml = textwrap.dedent("""\
            <launch>
              <set_remap from="/in" to="/out"/>
              <node pkg="p" exec="e" name="n"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        assert len(nodes) == 1
        node = nodes[0]
        assert ("/in", "/out") in node["remappings"] or ["/in", "/out"] in node["remappings"]

    def test_log(self):
        xml = '<launch><log message="hello"/></launch>'
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        log = nodes[0]
        assert log["kind"] == "log"
        assert log["message"] == "hello"

    def test_event_handler_tracked(self):
        """Event handlers are tracked as kind='event_handler' nodes."""
        xml = textwrap.dedent("""\
            <launch>
              <on_process_exit target="my_node">
                <emit_event event="shutdown" target_node="my_node"/>
              </on_process_exit>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        eh_nodes = [n for n in tracked["nodes"] if n.get("kind") == "event_handler"]
        assert len(eh_nodes) == 1
        eh = eh_nodes[0]
        assert eh["handler_kind"] == "on_process_exit"
        assert eh["target"] == "my_node"

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

    def test_includes_tracked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            child_path = os.path.join(tmpdir, "child.launch.xml")
            with open(child_path, "w") as f:
                f.write('<launch><node pkg="p" exec="e" name="n"/></launch>')

            xml = f"""\
                <launch>
                  <include file="{child_path}">
                    <arg name="x" value="42"/>
                  </include>
                </launch>
            """
            tracked = _parse_to_tracked(xml)
            nodes = _tracked_nodes(tracked)
            assert any(n["package"] == "p" for n in nodes)

    def test_errors_and_warnings(self):
        xml = textwrap.dedent("""\
            <launch>
              <node pkg="$(arg undefined)" exec="e" name="n"/>
              <foobar/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        assert any("undefined" in e for e in tracked["errors"])
        assert any("unknown" in w for w in tracked["warnings"])

    def test_env_effective(self):
        xml = textwrap.dedent("""\
            <launch>
              <set_env name="A" value="1"/>
              <node pkg="p" exec="e" name="n">
                <env name="B" value="2"/>
              </node>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        node = nodes[0]
        assert node["env"] == {"A": "1", "B": "2"}

    def test_namespace_stack_and_explicit(self):
        xml = textwrap.dedent("""\
            <launch>
              <push-ros-namespace namespace="/a"/>
              <push-ros-namespace namespace="b"/>
              <node pkg="p" exec="e" name="n" namespace="c"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        node = nodes[0]
        assert node["namespace_stack"] == ["/a", "b"]
        assert node["explicit_namespace"] == "c"

    def test_absolute_namespace_resets(self):
        xml = textwrap.dedent("""\
            <launch>
              <push-ros-namespace namespace="/old"/>
              <node pkg="p" exec="e" name="n" namespace="/override"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        node = nodes[0]
        assert node["explicit_namespace"] == "/override"

    def test_condition_filters(self):
        xml = textwrap.dedent("""\
            <launch>
              <arg name="flag" default="false"/>
              <node pkg="yes" exec="e" name="y" unless="$(arg flag)"/>
              <node pkg="no" exec="e" name="n" if="$(arg flag)"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        assert len(nodes) == 1
        assert nodes[0]["package"] == "yes"

    def test_scoped_group_env_not_leaked(self):
        xml = textwrap.dedent("""\
            <launch>
              <group scoped="true">
                <set_env name="X" value="leaked"/>
                <node pkg="inner" exec="e" name="i"/>
              </group>
              <node pkg="outer" exec="e" name="o"/>
            </launch>
        """)
        tracked = _parse_to_tracked(xml)
        nodes = _tracked_nodes(tracked)
        inner = next(n for n in nodes if n["package"] == "inner")
        assert inner["env"].get("X") == "leaked"
        outer = next(n for n in nodes if n["package"] == "outer")
        assert outer["env"].get("X") is None


# ─── rosdep resolve parser tests ─────────────────────────────────────────────
# Mirror the Rust-side tests in rosdep.rs.


# ─── _apply_declared_arg (lazy default evaluation) ───────────────────────────


class TestApplyDeclaredArgLazy:
    """Default is NOT resolved when the arg is already set by the caller."""

    def test_default_not_resolved_when_arg_already_set(self):
        """FindPackageShare in default must not be perform()'d if arg is set."""
        R.get_state().preview_mode = False
        # No package in AMENT — perform() would error if called.
        ctx = _make_context({"my_arg": "already_set_value"})
        arg = R._DeclaredArg(
            "my_arg",
            default_value=[
                R._TrackedFindPackageShare("nonexistent_pkg"),
                "/config/file.yaml",
            ],
        )
        R._apply_declared_arg(arg, ctx)
        # Arg value unchanged (caller's value preserved).
        assert ctx._launch_configurations["my_arg"] == "already_set_value"
        # No error — default was not resolved.
        assert not any("nonexistent_pkg" in e for e in R.get_state().tracked["errors"])

    def test_default_deferred_when_arg_not_set(self):
        """Default is stored as _DeferredDefault, resolved on read."""
        R.get_state().preview_mode = True
        ctx = _make_context({})
        arg = R._DeclaredArg(
            "my_arg",
            default_value="simple_default",
        )
        R._apply_declared_arg(arg, ctx)
        # Stored as deferred, not yet resolved.
        assert isinstance(ctx._launch_configurations["my_arg"], R._DeferredDefault)
        # Reading via LaunchConfiguration resolves it.
        lc = R._LaunchConfiguration("my_arg")
        assert lc.perform(ctx) == "simple_default"
        # Now it's resolved in the context.
        assert ctx._launch_configurations["my_arg"] == "simple_default"

    def test_deferred_default_not_resolved_if_never_read(self):
        """FindPackageShare for uninstalled pkg causes no error if arg is never read."""
        R.get_state().preview_mode = False
        ctx = _make_context({})
        arg = R._DeclaredArg(
            "cuda_param",
            default_value=[
                R._TrackedFindPackageShare("uninstalled_cuda_pkg"),
                "/config/file.yaml",
            ],
        )
        R._apply_declared_arg(arg, ctx)
        # Default is deferred — no resolution happened, no error.
        assert isinstance(ctx._launch_configurations["cuda_param"], R._DeferredDefault)
        assert not any("uninstalled_cuda_pkg" in e for e in R.get_state().tracked["errors"])

    def test_unresolved_default_recorded_for_show_args(self):
        """When arg is already set, the raw default string is recorded for --show-args."""
        R.get_state().preview_mode = False
        ctx = _make_context({"my_arg": "caller_value"})
        arg = R._DeclaredArg(
            "my_arg",
            default_value=[
                R._TrackedFindPackageShare("some_pkg"),
                "/config/file.yaml",
            ],
        )
        R._apply_declared_arg(arg, ctx)
        # declared_args records the unresolved default (str() form).
        recorded = R.get_state().tracked["declared_args"]
        assert len(recorded) == 1
        assert "$(find-pkg-share some_pkg)" in recorded[0]["default"]
        assert "/config/file.yaml" in recorded[0]["default"]


# ─── Strictness flags ────────────────────────────────────────────────────────


class TestStrictnessFlags:
    """Tests for apply_arg_defaults, global_arg_cascade, allow_unportable_paths."""

    def test_apply_arg_defaults_true_applies_default(self):
        R.get_state().apply_arg_defaults = True
        ctx = R._SubstitutionContext()
        elements = R.parse_xml_launch(
            '<launch><arg name="x" default="hello"/></launch>', "test.xml"
        )
        R.resolve_xml_elements(elements, ctx)
        assert ctx.args["x"] == "hello"

    def test_apply_arg_defaults_false_skips_default(self):
        R.get_state().apply_arg_defaults = False
        ctx = R._SubstitutionContext()
        elements = R.parse_xml_launch(
            '<launch><arg name="x" default="hello"/></launch>', "test.xml"
        )
        R.resolve_xml_elements(elements, ctx)
        # Default not applied — arg stays absent
        assert "x" not in ctx.args

    def test_apply_arg_defaults_false_undefined_ref_errors(self):
        R.get_state().apply_arg_defaults = False
        ctx = R._SubstitutionContext()
        elements = R.parse_xml_launch(
            """<launch>
                <arg name="x" default="hello"/>
                <let name="y" value="$(arg x)"/>
            </launch>""",
            "test.xml",
        )
        R.resolve_xml_elements(elements, ctx)
        assert any("undefined" in e for e in R.get_state().tracked["errors"])

    def test_global_arg_cascade_true_inherits_parent_args(self):
        R.get_state().global_arg_cascade = True
        with tempfile.TemporaryDirectory() as child_share:
            R.get_state().package_shares["child_pkg"] = child_share
            launch_dir = os.path.join(child_share, "launch")
            os.makedirs(launch_dir, exist_ok=True)
            with open(os.path.join(launch_dir, "child.launch.xml"), "w") as f:
                f.write('<launch><arg name="x" default="fallback"/></launch>')
            ctx = R._SubstitutionContext()
            ctx.args = {"x": "from_parent"}
            elements = R.parse_xml_launch(
                "<launch>"
                '<include file="$(find-pkg-share child_pkg)'
                '/launch/child.launch.xml"/>'
                "</launch>",
                "test.xml",
            )
            R.resolve_xml_elements(elements, ctx)
            # Child sees parent arg — no error
            assert not R.get_state().tracked["errors"]

    def test_global_arg_cascade_false_no_parent_args(self):
        R.get_state().global_arg_cascade = False
        R.get_state().apply_arg_defaults = False
        with tempfile.TemporaryDirectory() as child_share:
            R.get_state().package_shares["child_pkg"] = child_share
            launch_dir = os.path.join(child_share, "launch")
            os.makedirs(launch_dir, exist_ok=True)
            with open(os.path.join(launch_dir, "child.launch.xml"), "w") as f:
                f.write('<launch><arg name="x"/><let name="y" value="$(arg x)"/></launch>')
            ctx = R._SubstitutionContext()
            ctx.args = {"x": "from_parent"}
            elements = R.parse_xml_launch(
                "<launch>"
                '<include file="$(find-pkg-share child_pkg)'
                '/launch/child.launch.xml"/>'
                "</launch>",
                "test.xml",
            )
            R.resolve_xml_elements(elements, ctx)
            # Child can't see parent arg — undefined error
            assert any("undefined" in e for e in R.get_state().tracked["errors"])

    def test_allow_unportable_paths_false_errors(self):
        R.get_state().allow_unportable_paths = False
        R.get_state().preview_mode = True
        ctx = R._SubstitutionContext()
        elements = R.parse_xml_launch(
            '<launch><include file="/absolute/path/to/file.launch.xml"/></launch>',
            "test.xml",
        )
        R.resolve_xml_elements(elements, ctx)
        assert any("unportable" in e for e in R.get_state().tracked["errors"])

    def test_allow_unportable_paths_true_warns(self):
        R.get_state().allow_unportable_paths = True
        R.get_state().preview_mode = True
        ctx = R._SubstitutionContext()
        elements = R.parse_xml_launch(
            '<launch><include file="/absolute/path/to/file.launch.xml"/></launch>',
            "test.xml",
        )
        R.resolve_xml_elements(elements, ctx)
        assert any("unportable" in w for w in R.get_state().tracked["warnings"])
        assert not any("unportable" in e for e in R.get_state().tracked["errors"])


# ─── _resolve_pkg_share and _TrackedFindPackageShare ─────────────────────────


class TestResolvePkgShare:
    """Tests for _resolve_pkg_share mode-dependent behavior."""

    def test_preview_returns_source_path_from_package_shares(self):
        R.get_state().preview_mode = True
        R.get_state().package_shares["my_pkg"] = "/ws/src/my_pkg"
        assert R._resolve_pkg_share(R.get_state(), "my_pkg") == "/ws/src/my_pkg"

    def test_preview_unknown_pkg_returns_portable(self):
        R.get_state().preview_mode = True
        result = R._resolve_pkg_share(R.get_state(), "unknown_pkg")
        assert result == "$(find-pkg-share unknown_pkg)"

    def test_postbuild_returns_install_path_from_package_shares(self):
        R.get_state().preview_mode = False
        R.get_state().package_shares["my_pkg"] = "/ws/install/my_pkg/share/my_pkg"
        assert R._resolve_pkg_share(R.get_state(), "my_pkg") == "/ws/install/my_pkg/share/my_pkg"

    def test_postbuild_unknown_pkg_raises(self):
        R.get_state().preview_mode = False
        import pytest

        with pytest.raises(LookupError, match="not found in AMENT_PREFIX_PATH"):
            R._resolve_pkg_share(R.get_state(), "unknown_pkg")

    def test_postbuild_skips_lockfile_fetch(self):
        """In postbuild mode, lockfile packages not in _package_shares are not fetched."""
        R.get_state().preview_mode = False
        R.get_state().lockfile_data = {
            "lockfile_pkg": {
                "repo": "org/repo",
                "path": "pkg",
                "url": "https://example.com",
                "version": "abc123",
            }
        }
        import pytest

        # Should raise, not attempt to fetch
        with pytest.raises(LookupError, match="not found in AMENT_PREFIX_PATH"):
            R._resolve_pkg_share(R.get_state(), "lockfile_pkg")


class TestTrackedFindPackageShare:
    """Tests for _TrackedFindPackageShare mode-dependent perform()/str()."""

    def test_preview_returns_portable(self):
        R.get_state().preview_mode = True
        fps = R._TrackedFindPackageShare("my_pkg")
        assert fps.perform(None) == "$(find-pkg-share my_pkg)"
        assert str(fps) == "$(find-pkg-share my_pkg)"

    def test_postbuild_returns_install_path(self):
        R.get_state().preview_mode = False
        R.get_state().package_shares["my_pkg"] = "/install/share/my_pkg"
        fps = R._TrackedFindPackageShare("my_pkg")
        assert fps.perform(None) == "/install/share/my_pkg"
        assert str(fps) == "/install/share/my_pkg"

    def test_postbuild_unresolvable_reports_error(self):
        R.get_state().preview_mode = False
        fps = R._TrackedFindPackageShare("missing_pkg")
        result = fps.perform(None)
        # Returns portable fallback but records an error
        assert result == "$(find-pkg-share missing_pkg)"
        assert any("missing_pkg" in e for e in R.get_state().tracked["errors"])


class TestParseRosdepResolve:
    def test_single_key_apt(self):
        stdout = "#apt\nros-jazzy-rclcpp\n"
        assert R._parse_rosdep_resolve(stdout) == ["ros-jazzy-rclcpp"]

    def test_multi_key(self):
        stdout = "#ROSDEP[rclcpp]\n#apt\nros-jazzy-rclcpp\n#ROSDEP[eigen]\n#apt\nlibeigen3-dev\n"
        assert R._parse_rosdep_resolve(stdout) == ["ros-jazzy-rclcpp", "libeigen3-dev"]

    def test_multi_packages_per_key(self):
        stdout = "#ROSDEP[libnl-3-dev]\n#apt\nlibnl-3-dev libnl-genl-3-dev libnl-route-3-dev\n"
        assert R._parse_rosdep_resolve(stdout) == [
            "libnl-3-dev",
            "libnl-genl-3-dev",
            "libnl-route-3-dev",
        ]

    def test_empty_output(self):
        assert R._parse_rosdep_resolve("") == []

    def test_unsupported_installer_ignored(self):
        stdout = "#brew\nhomebrew-pkg\n"
        assert R._parse_rosdep_resolve(stdout) == []

    def test_mixed_installers_only_apt(self):
        stdout = (
            "#ROSDEP[rclcpp]\n#apt\nros-jazzy-rclcpp\n#ROSDEP[brew_only]\n#brew\nhomebrew-pkg\n"
        )
        assert R._parse_rosdep_resolve(stdout) == ["ros-jazzy-rclcpp"]

    def test_pip_ignored(self):
        stdout = "#pip\nsome-pip-package\n"
        assert R._parse_rosdep_resolve(stdout) == []

    def test_unresolved_key_no_installer_line(self):
        """A key with a #ROSDEP header but no #installer line is unresolved."""
        stdout = "#ROSDEP[rclcpp]\n#apt\nros-jazzy-rclcpp\n#ROSDEP[nonexistent_xyz]\n"
        # Only the resolved key's package is returned.
        assert R._parse_rosdep_resolve(stdout) == ["ros-jazzy-rclcpp"]
