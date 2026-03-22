"""Tests for launch_plus.indexer — ported from indexer.rs Rust tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from launch_plus.indexer import (
    _evaluate_condition_impl,
    compute_build_order,
    evaluate_condition,
    parse_lockfile,
    parse_package_xml,
    parse_repos,
    resolve_dependencies,
    serialize_lockfile,
)
from launch_plus.types import (
    DependencyMode,
    Lockfile,
    PackageLock,
    RepoLock,
)

# =========================================================================
# .repos parsing
# =========================================================================


def test_parse_repos() -> None:
    content = """\
repositories:
  core/autoware_msgs:
    type: git
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 1.11.0
"""
    repos = parse_repos(content)
    assert len(repos.repositories) == 1
    entry = repos.repositories["core/autoware_msgs"]
    assert entry.repo_type == "git"
    assert entry.version == "1.11.0"


def test_parse_repos_multiple() -> None:
    content = """\
repositories:
  core/autoware_msgs:
    type: git
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 1.11.0
  universe/autoware_utils:
    type: git
    url: https://github.com/autowarefoundation/autoware_utils.git
    version: main
"""
    repos = parse_repos(content)
    assert len(repos.repositories) == 2
    assert "core/autoware_msgs" in repos.repositories
    assert "universe/autoware_utils" in repos.repositories


# =========================================================================
# Lockfile roundtrip
# =========================================================================


def test_lockfile_roundtrip() -> None:
    lockfile = Lockfile()
    lockfile.repositories["core/autoware_msgs"] = RepoLock(
        url="https://github.com/autowarefoundation/autoware_msgs.git",
        version="abc123def456",
        version_ref="1.11.0",
        packages=["autoware_msgs"],
    )
    lockfile.packages["autoware_msgs"] = PackageLock(
        repo="core/autoware_msgs",
        path="autoware_msgs",
    )

    yaml_str = serialize_lockfile(lockfile)
    parsed = parse_lockfile(yaml_str)

    assert len(parsed.repositories) == 1
    assert len(parsed.packages) == 1
    repo = parsed.repositories["core/autoware_msgs"]
    assert repo.repo_type == "git"
    assert repo.version == "abc123def456"
    assert repo.version_ref == "1.11.0"


def test_remove_repo() -> None:
    lockfile = Lockfile()

    lockfile.repositories["core/msgs"] = RepoLock(
        url="https://example.com/msgs.git",
        version="abc123",
        version_ref="1.0.0",
        packages=["pkg_a", "pkg_b"],
    )
    lockfile.repositories["universe/utils"] = RepoLock(
        url="https://example.com/utils.git",
        version="def456",
        version_ref="2.0.0",
        packages=["pkg_c"],
    )

    lockfile.packages["pkg_a"] = PackageLock(repo="core/msgs", path="pkg_a")
    lockfile.packages["pkg_b"] = PackageLock(repo="core/msgs", path="pkg_b")
    lockfile.packages["pkg_c"] = PackageLock(repo="universe/utils", path="pkg_c")

    assert len(lockfile.repositories) == 2
    assert len(lockfile.packages) == 3

    lockfile.remove_repo("core/msgs")

    assert len(lockfile.repositories) == 1
    assert len(lockfile.packages) == 1
    assert "core/msgs" not in lockfile.repositories
    assert "universe/utils" in lockfile.repositories
    assert "pkg_a" not in lockfile.packages
    assert "pkg_b" not in lockfile.packages
    assert "pkg_c" in lockfile.packages


# =========================================================================
# package.xml parsing
# =========================================================================


def test_parse_package_xml_basic() -> None:
    content = """\
<?xml version="1.0"?>
<package format="3">
  <name>my_package</name>
  <version>1.0.0</version>
  <description>A test package</description>
  <maintainer email="test@example.com">Test</maintainer>
  <license>Apache-2.0</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <build_depend>rclcpp</build_depend>
  <exec_depend>std_msgs</exec_depend>
  <test_depend>ament_lint_auto</test_depend>
</package>
"""
    info = parse_package_xml(content, "my_package")
    assert info.name == "my_package"
    assert info.path == "my_package"
    assert "ament_cmake" in info.dependencies.buildtool
    assert "rclcpp" in info.dependencies.build
    assert "std_msgs" in info.dependencies.exec
    assert "ament_lint_auto" in info.dependencies.test


def test_parse_package_xml_depend() -> None:
    content = """\
<?xml version="1.0"?>
<package format="3">
  <name>sensor_pkg</name>
  <depend>sensor_msgs</depend>
  <depend>geometry_msgs</depend>
</package>
"""
    info = parse_package_xml(content, "sensor_pkg")
    assert info.name == "sensor_pkg"
    # <depend> should add to build, build_export, and exec
    assert "sensor_msgs" in info.dependencies.build
    assert "geometry_msgs" in info.dependencies.build
    assert "sensor_msgs" in info.dependencies.build_export
    assert "geometry_msgs" in info.dependencies.build_export
    assert "sensor_msgs" in info.dependencies.exec
    assert "geometry_msgs" in info.dependencies.exec


def test_parse_package_xml_missing_name() -> None:
    content = """\
<?xml version="1.0"?>
<package format="3">
  <version>1.0.0</version>
</package>
"""
    try:
        parse_package_xml(content, "test")
        assert False, "should have raised"  # noqa: B011
    except Exception:
        pass


def test_parse_package_xml_condition_filters_deps() -> None:
    content = """\
<?xml version="1.0"?>
<package format="3">
  <name>test_pkg</name>
  <buildtool_depend condition="1 == 2">excluded_dep</buildtool_depend>
  <buildtool_depend condition="1 == 1">included_dep</buildtool_depend>
  <depend>unconditional_dep</depend>
</package>
"""
    info = parse_package_xml(content, "test")
    assert "excluded_dep" not in info.dependencies.buildtool
    assert "included_dep" in info.dependencies.buildtool
    assert "unconditional_dep" in info.dependencies.build


def test_parse_package_xml_no_condition_includes_all() -> None:
    content = """\
<?xml version="1.0"?>
<package format="3">
  <name>test_pkg</name>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <build_depend>rclcpp</build_depend>
  <exec_depend>std_msgs</exec_depend>
</package>
"""
    info = parse_package_xml(content, "test")
    assert "ament_cmake" in info.dependencies.buildtool
    assert "rclcpp" in info.dependencies.build
    assert "std_msgs" in info.dependencies.exec


# =========================================================================
# REP-149 condition evaluator
# =========================================================================


def _eval_with(condition: str, env: dict[str, str]) -> bool:
    return _evaluate_condition_impl(condition, env.get)


def test_condition_simple_eq_true() -> None:
    assert evaluate_condition("1 == 1")


def test_condition_simple_eq_false() -> None:
    assert not evaluate_condition("1 == 2")


def test_condition_simple_ne() -> None:
    assert evaluate_condition("1 != 2")
    assert not evaluate_condition("1 != 1")


def test_condition_string_comparison_operators() -> None:
    assert evaluate_condition("a < b")
    assert evaluate_condition("a <= b")
    assert evaluate_condition("a <= a")
    assert evaluate_condition("b > a")
    assert evaluate_condition("b >= a")
    assert evaluate_condition("b >= b")
    assert not evaluate_condition("b < a")


def test_condition_logical_and() -> None:
    assert evaluate_condition("1 == 1 and 2 == 2")
    assert not evaluate_condition("1 == 1 and 1 == 2")


def test_condition_logical_or() -> None:
    assert evaluate_condition("1 == 2 or 2 == 2")
    assert not evaluate_condition("1 == 2 or 3 == 4")


def test_condition_and_binds_tighter_than_or() -> None:
    assert evaluate_condition("1 == 2 and 1 == 1 or 2 == 2")
    assert evaluate_condition("1 == 1 or 1 == 2 and 1 == 2")


def test_condition_parentheses() -> None:
    assert not evaluate_condition("(1 == 1 or 1 == 2) and 1 == 2")


def test_condition_quoted_strings() -> None:
    assert evaluate_condition('"hello" == "hello"')
    assert not evaluate_condition('"hello" == "world"')
    assert evaluate_condition("'foo' == 'foo'")


def test_condition_bare_word_with_dash() -> None:
    assert evaluate_condition("my-value == my-value")
    assert not evaluate_condition("my-value == other-value")


def test_condition_unset_var_is_empty_string() -> None:
    env: dict[str, str] = {}
    assert _eval_with("$MISSING_VAR == ''", env)
    assert not _eval_with("$MISSING_VAR == 1", env)


def test_condition_nested_parentheses() -> None:
    assert evaluate_condition("((1 == 1))")


def test_condition_empty() -> None:
    assert evaluate_condition("")


def test_condition_error_on_garbage() -> None:
    try:
        evaluate_condition("@#!")
        assert False, "should have raised"  # noqa: B011
    except ValueError:
        pass


def test_condition_error_on_unbalanced_paren() -> None:
    try:
        evaluate_condition("(1 == 1")
        assert False, "should have raised"  # noqa: B011
    except ValueError:
        pass


# =========================================================================
# Dependency resolution
# =========================================================================


def _make_test_lockfile_with_deps() -> tuple[Lockfile, Path]:
    """Create a lockfile and temp dir with package.xml files for dep testing."""
    tmp = tempfile.mkdtemp()
    tmp_path = Path(tmp)

    lockfile = Lockfile()
    lockfile.repositories["repo"] = RepoLock(
        url="https://example.com/repo.git",
        version="abc123",
        packages=["pkg_a", "pkg_b", "pkg_c"],
    )
    lockfile.packages["pkg_a"] = PackageLock(repo="repo", path="pkg_a")
    lockfile.packages["pkg_b"] = PackageLock(repo="repo", path="pkg_b")
    lockfile.packages["pkg_c"] = PackageLock(repo="repo", path="pkg_c")

    # pkg_a depends on pkg_b (build)
    pkg_a_dir = tmp_path / "repo" / "pkg_a"
    pkg_a_dir.mkdir(parents=True)
    (pkg_a_dir / "package.xml").write_text(
        '<?xml version="1.0"?>\n<package format="3">\n'
        "  <name>pkg_a</name>\n  <build_depend>pkg_b</build_depend>\n</package>\n"
    )

    # pkg_b depends on pkg_c (exec)
    pkg_b_dir = tmp_path / "repo" / "pkg_b"
    pkg_b_dir.mkdir(parents=True)
    (pkg_b_dir / "package.xml").write_text(
        '<?xml version="1.0"?>\n<package format="3">\n'
        "  <name>pkg_b</name>\n  <exec_depend>pkg_c</exec_depend>\n</package>\n"
    )

    # pkg_c has no deps
    pkg_c_dir = tmp_path / "repo" / "pkg_c"
    pkg_c_dir.mkdir(parents=True)
    (pkg_c_dir / "package.xml").write_text(
        '<?xml version="1.0"?>\n<package format="3">\n  <name>pkg_c</name>\n</package>\n'
    )

    return lockfile, tmp_path


def test_resolve_dependencies_build() -> None:
    lockfile, src_dir = _make_test_lockfile_with_deps()
    graph = resolve_dependencies(lockfile, src_dir, {"pkg_a"}, DependencyMode.BUILD)
    assert "pkg_a" in graph.packages
    assert "pkg_b" in graph.packages
    # pkg_c is only an exec dep of pkg_b, not reachable via BUILD mode
    assert "pkg_c" not in graph.packages


def test_resolve_dependencies_all() -> None:
    lockfile, src_dir = _make_test_lockfile_with_deps()
    graph = resolve_dependencies(lockfile, src_dir, {"pkg_a"}, DependencyMode.ALL)
    assert "pkg_a" in graph.packages
    assert "pkg_b" in graph.packages
    assert "pkg_c" in graph.packages


def test_resolve_dependencies_external() -> None:
    lockfile, src_dir = _make_test_lockfile_with_deps()
    graph = resolve_dependencies(lockfile, src_dir, {"nonexistent"}, DependencyMode.BUILD)
    assert "nonexistent" in graph.external


def test_compute_build_order() -> None:
    lockfile, src_dir = _make_test_lockfile_with_deps()
    order = compute_build_order(lockfile, src_dir, {"pkg_a", "pkg_b", "pkg_c"})
    # pkg_b must come before pkg_a (pkg_a build-depends on pkg_b)
    assert order.index("pkg_b") < order.index("pkg_a")
