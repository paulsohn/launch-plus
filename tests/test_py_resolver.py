"""Tests for py_resolver internals — substitution handling, node tracking,
and unresolved substitution tracking."""

import os
import textwrap
import tempfile

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


# ─── _resolve_substitution ───────────────────────────────────────────────────

class TestResolveSubstitution:
    def test_resolves_launch_configuration(self):
        lc = R._LaunchConfiguration("my_var")
        ctx = _make_context({"my_var": "resolved_value"})
        assert R._resolve_substitution(lc, ctx) == "resolved_value"

    def test_unresolved_falls_back_to_sentinel(self):
        lc = R._LaunchConfiguration("missing")
        ctx = _make_context({})
        # perform() returns None → sentinel for LaunchConfiguration
        assert R._resolve_substitution(lc, ctx) == "${{unresolved:missing}}"

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
        # Unresolved element produces sentinel for LaunchConfiguration
        assert R._resolve_substitution(parts, ctx) == "abc/${{unresolved:unresolved_var}}"


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

    def test_tracked_node_unresolved_package_stays_as_sentinel(self):
        """When the LaunchConfiguration cannot be resolved (not in context),
        the entry keeps the sentinel but does NOT track it as a package."""
        ctx = _make_context({})
        node = R._TrackedNode(
            package=R._LaunchConfiguration("unknown_pkg"),
            executable="exec",
        )
        R._resolve_node_details(node, ctx)

        # The entry shows the sentinel (not the plain variable name)
        assert R._tracked["nodes"][node._idx]["package"] == "${{unresolved:unknown_pkg}}"
        # It must NOT be tracked as a real package dependency
        assert "unknown_pkg" not in R._tracked["packages"]

    def test_tracked_container_resolves_all_fields(self):
        ctx = _make_context({
            "pkg": "rclcpp_components",
            "exe": "component_container_mt",
            "cname": "my_container",
        })
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
        assert plugins[0]["package"] == "${{unresolved:unknown}}"  # sentinel
        assert "unknown" not in R._tracked["packages"]


# ─── Unresolved substitution tracking ────────────────────────────────────────

class TestUnresolvedSubstitutions:
    def test_unresolved_launch_config_tracked(self):
        """Node with LaunchConfiguration("x") for package where x is not
        in the context should produce an unresolved_substitutions entry."""
        lc = R._LaunchConfiguration("container_package")
        node = R._TrackedNode(package=lc, executable="my_exec")

        ctx = _make_context({})  # container_package NOT in context
        R._resolve_node_details(node, ctx)

        unsubs = R._tracked["unresolved_substitutions"]
        assert len(unsubs) == 1
        assert unsubs[0]["node_idx"] == node._idx
        assert unsubs[0]["field"] == "package"
        assert unsubs[0]["variable_name"] == "container_package"
        assert "${{unresolved:container_package}}" in unsubs[0]["full_template"]

    def test_no_unresolved_when_variable_set(self):
        """When the variable IS in the context, no unresolved_substitutions entry."""
        lc = R._LaunchConfiguration("container_package")
        node = R._TrackedNode(package=lc, executable="my_exec")

        ctx = _make_context({"container_package": "rclcpp_components"})
        R._resolve_node_details(node, ctx)

        unsubs = R._tracked["unresolved_substitutions"]
        assert len(unsubs) == 0
        # And the node's package should be resolved
        entry = R._tracked["nodes"][node._idx]
        assert entry["package"] == "rclcpp_components"

    def test_unresolved_sentinel_format(self):
        """The sentinel ${{unresolved:x}} should appear in the node's tracked field."""
        lc = R._LaunchConfiguration("my_var")
        node = R._TrackedNode(package=lc, executable="my_exec")

        ctx = _make_context({})  # my_var NOT in context
        R._resolve_node_details(node, ctx)

        entry = R._tracked["nodes"][node._idx]
        assert entry["package"] == "${{unresolved:my_var}}"

    def test_set_launch_configuration_still_recorded(self):
        """SetLaunchConfiguration should still be recorded in
        _tracked['set_launch_configurations']."""
        slc = R._SetLaunchConfiguration("my_key", "my_value")

        ctx = _make_context({})
        R._walk_action(slc, ctx, depth=0)

        assert R._tracked["set_launch_configurations"]["my_key"] == "my_value"
        assert ctx._launch_configurations["my_key"] == "my_value"

    def test_unresolved_in_executable_field(self):
        """Unresolved LaunchConfiguration in executable field should also be tracked."""
        lc = R._LaunchConfiguration("my_executable")
        node = R._TrackedNode(package="my_pkg", executable=lc)

        ctx = _make_context({})
        R._resolve_node_details(node, ctx)

        unsubs = R._tracked["unresolved_substitutions"]
        assert len(unsubs) == 1
        assert unsubs[0]["field"] == "executable"
        assert unsubs[0]["variable_name"] == "my_executable"

    def test_unresolved_not_tracked_as_package(self):
        """An unresolved LaunchConfiguration for a package field should NOT
        be tracked in _tracked['packages'] (it's not a real package name)."""
        lc = R._LaunchConfiguration("container_package")
        node = R._TrackedNode(package=lc, executable="my_exec")

        ctx = _make_context({})
        R._resolve_node_details(node, ctx)

        # The sentinel should not be in packages
        assert "${{unresolved:container_package}}" not in R._tracked["packages"]
        assert "container_package" not in R._tracked["packages"]
