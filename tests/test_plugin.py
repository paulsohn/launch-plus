"""Tests for connection metadata plugin: call_plugin validation, node and
composable integration via apply_connection_plugin."""

import pytest

from roscope.entities.helpers import _expand_connection_name
from roscope.entities.launch_context import LaunchContext
from roscope.plugin import call_plugin

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_plugin_fn(connections: dict):
    """Return a get_connections() callable that always returns *connections*."""

    def get_connections(
        *,
        pkg_share_path,
        params,
        pkg_name,
        executable=None,
        plugin_name=None,
        args=None,
    ):
        return connections

    return get_connections


def _make_context(plugin_fn=None, pkg_shares=None):
    ctx = LaunchContext()
    ctx._state.preview_mode = True
    ctx._state.connection_plugin = plugin_fn
    if pkg_shares:
        ctx._state.package_shares.update(pkg_shares)
    return ctx


# ─── call_plugin ──────────────────────────────────────────────────────────────


class TestCallPlugin:
    def test_returns_empty_when_plugin_fn_none(self):
        result = call_plugin(None, pkg_share_path="/p", params={}, pkg_name="pkg", executable="n")
        assert result == {}

    def test_exactly_one_required_both_given(self):
        fn = _make_plugin_fn({})
        with pytest.raises(ValueError, match="exactly one"):
            call_plugin(
                fn,
                pkg_share_path="/p",
                params={},
                pkg_name="pkg",
                executable="n",
                plugin_name="P::N",
            )

    def test_exactly_one_required_neither_given(self):
        fn = _make_plugin_fn({})
        with pytest.raises(ValueError, match="exactly one"):
            call_plugin(fn, pkg_share_path="/p", params={}, pkg_name="pkg")

    def test_valid_connections_returned(self):
        connections = {"~/topic": {"type": "publisher"}}
        fn = _make_plugin_fn(connections)
        result = call_plugin(fn, pkg_share_path="/p", params={}, pkg_name="pkg", executable="n")
        assert result == connections

    def test_non_string_key_skipped(self):
        fn = _make_plugin_fn({42: {"type": "publisher"}, "/ok": {"type": "subscription"}})
        result = call_plugin(fn, pkg_share_path="/p", params={}, pkg_name="pkg", executable="n")
        assert list(result.keys()) == ["/ok"]

    def test_non_dict_meta_skipped(self):
        fn = _make_plugin_fn({"/bad": "not-a-dict", "/ok": {"type": "subscription"}})
        result = call_plugin(fn, pkg_share_path="/p", params={}, pkg_name="pkg", executable="n")
        assert list(result.keys()) == ["/ok"]

    def test_unrecognized_type_skipped(self):
        fn = _make_plugin_fn({"/a": {"type": "unknown"}, "/b": {"type": "publisher"}})
        result = call_plugin(fn, pkg_share_path="/p", params={}, pkg_name="pkg", executable="n")
        assert list(result.keys()) == ["/b"]

    def test_plugin_exception_returns_empty(self):
        def bad_fn(**kwargs):
            raise RuntimeError("boom")

        result = call_plugin(bad_fn, pkg_share_path="/p", params={}, pkg_name="pkg", executable="n")
        assert result == {}

    def test_plugin_returns_non_dict_returns_empty(self):
        fn = lambda **kw: ["not", "a", "dict"]  # noqa: E731
        result = call_plugin(fn, pkg_share_path="/p", params={}, pkg_name="pkg", executable="n")
        assert result == {}


# ─── _expand_connection_name ──────────────────────────────────────────────────


class TestExpandConnectionName:
    def test_absolute_unchanged(self):
        assert _expand_connection_name("/abs/topic", "/ns", "node") == "/abs/topic"

    def test_private_with_ns_and_name(self):
        assert _expand_connection_name("~/out", "/ns", "node") == "/ns/node/out"

    def test_private_name_only(self):
        assert _expand_connection_name("~/out", None, "node") == "/node/out"

    def test_private_ns_only(self):
        assert _expand_connection_name("~/out", "/ns", None) == "/out"

    def test_relative_with_ns(self):
        assert _expand_connection_name("out", "/ns", "node") == "/ns/out"

    def test_relative_no_ns(self):
        assert _expand_connection_name("out", None, None) == "/out"


# ─── apply_connection_plugin: Node ────────────────────────────────────────────

_PKG_SHARES = {"my_pkg": "/fake/share"}


class TestApplyConnectionPluginNode:
    def test_plugin_connections_added_to_remaps(self):
        connections = {"~/input": {"type": "subscription"}}
        ctx = _make_context(_make_plugin_fn(connections), _PKG_SHARES)
        remaps = []
        ctx._state.apply_connection_plugin(
            "my_pkg",
            {},
            remaps,
            executable="my_node",
            node_ns="/ns",
            node_name="my_node",
        )
        assert ["~/input", "~/input"] in remaps

    def test_metadata_preserved_in_return(self):
        connections = {"~/out": {"type": "publisher", "qos": {"reliability": "reliable"}}}
        ctx = _make_context(_make_plugin_fn(connections), _PKG_SHARES)
        remaps = []
        meta = ctx._state.apply_connection_plugin(
            "my_pkg",
            {},
            remaps,
            executable="my_node",
            node_ns="/ns",
            node_name="my_node",
        )
        assert meta["~/out"]["type"] == "publisher"
        assert meta["~/out"]["qos"]["reliability"] == "reliable"

    def test_existing_remap_not_duplicated(self):
        connections = {"~/input": {"type": "subscription"}}
        ctx = _make_context(_make_plugin_fn(connections), _PKG_SHARES)
        remaps = [["~/input", "/remapped/input"]]
        ctx._state.apply_connection_plugin(
            "my_pkg",
            {},
            remaps,
            executable="my_node",
            node_ns="/ns",
            node_name="my_node",
        )
        assert remaps.count(["~/input", "~/input"]) == 0
        assert len([r for r in remaps if r[0] == "~/input"]) == 1

    def test_no_plugin_returns_empty(self):
        ctx = _make_context(None)
        result = ctx._state.apply_connection_plugin("my_pkg", {}, [], executable="my_node")
        assert result == {}

    def test_invalid_plugin_output_ignored(self):
        fn = _make_plugin_fn({42: {"type": "publisher"}})
        ctx = _make_context(fn, _PKG_SHARES)
        remaps = []
        meta = ctx._state.apply_connection_plugin(
            "my_pkg",
            {},
            remaps,
            executable="my_node",
            node_ns="/ns",
            node_name="my_node",
        )
        assert meta == {}
        assert remaps == []

    def test_args_passed_to_plugin(self):
        received = {}

        def capturing_fn(
            *,
            pkg_share_path,
            params,
            pkg_name,
            executable=None,
            plugin_name=None,
            args=None,
        ):
            received["args"] = args
            return {}

        ctx = _make_context(capturing_fn, _PKG_SHARES)
        ctx._state.apply_connection_plugin(
            "my_pkg",
            {},
            [],
            executable="my_node",
            args=["/in", "/out"],
        )
        assert received["args"] == ["/in", "/out"]


# ─── apply_connection_plugin: composable node ─────────────────────────────────


class TestApplyConnectionPluginComposable:
    def test_composable_plugin_name_passed_through(self):
        received = {}

        def capturing_fn(
            *, pkg_share_path, params, pkg_name, executable=None, plugin_name=None, args=None
        ):
            received["plugin_name"] = plugin_name
            return {}

        ctx = _make_context(capturing_fn, _PKG_SHARES)
        ctx._state.apply_connection_plugin(
            "my_pkg",
            {},
            [],
            plugin_name="my_pkg::MyComponent",
            node_ns="/ns",
            node_name="MyComponent",
        )
        assert received["plugin_name"] == "my_pkg::MyComponent"

    def test_composable_metadata_flows_through(self):
        connections = {"/topic": {"type": "subscription"}}
        ctx = _make_context(_make_plugin_fn(connections), _PKG_SHARES)
        remaps = []
        meta = ctx._state.apply_connection_plugin(
            "my_pkg",
            {},
            remaps,
            plugin_name="my_pkg::MyComp",
            node_ns="/ns",
            node_name="MyComp",
        )
        assert "/topic" in meta
        assert ["/topic", "/topic"] in remaps
