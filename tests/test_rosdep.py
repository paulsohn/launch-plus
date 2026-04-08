"""Tests for roscope.rosdep — ported from rosdep.rs Rust tests."""

from __future__ import annotations

from roscope.rosdep import _parse_rosdep_resolve


def test_parse_single_key_apt() -> None:
    stdout = "#apt\nros-jazzy-rclcpp\n"
    result = _parse_rosdep_resolve(stdout, ["rclcpp"])
    assert result.apt == ["ros-jazzy-rclcpp"]
    assert result.pip == []
    assert result.unresolved == []


def test_parse_multi_key() -> None:
    stdout = "#ROSDEP[rclcpp]\n#apt\nros-jazzy-rclcpp\n#ROSDEP[eigen]\n#apt\nlibeigen3-dev\n"
    result = _parse_rosdep_resolve(stdout, ["rclcpp", "eigen"])
    assert result.apt == ["ros-jazzy-rclcpp", "libeigen3-dev"]
    assert result.unresolved == []


def test_parse_unresolved_key() -> None:
    stdout = "#ROSDEP[rclcpp]\n#apt\nros-jazzy-rclcpp\n#ROSDEP[nonexistent_xyz]\n"
    result = _parse_rosdep_resolve(stdout, ["rclcpp", "nonexistent_xyz"])
    assert result.apt == ["ros-jazzy-rclcpp"]
    assert result.unresolved == ["nonexistent_xyz"]


def test_parse_multi_packages_per_key() -> None:
    stdout = "#ROSDEP[libnl-3-dev]\n#apt\nlibnl-3-dev libnl-genl-3-dev libnl-route-3-dev\n"
    result = _parse_rosdep_resolve(stdout, ["libnl-3-dev"])
    assert result.apt == ["libnl-3-dev", "libnl-genl-3-dev", "libnl-route-3-dev"]


def test_parse_empty_output() -> None:
    result = _parse_rosdep_resolve("", ["foo", "bar"])
    assert result.unresolved == ["foo", "bar"]


def test_parse_unsupported_installer_single_key() -> None:
    stdout = "#brew\nhomebrew-pkg\n"
    result = _parse_rosdep_resolve(stdout, ["some_key"])
    assert result.apt == []
    assert result.pip == []
    assert result.unresolved == ["some_key"]


def test_parse_unsupported_installer_multi_key() -> None:
    stdout = "#ROSDEP[rclcpp]\n#apt\nros-jazzy-rclcpp\n#ROSDEP[brew_only]\n#brew\nhomebrew-pkg\n"
    result = _parse_rosdep_resolve(stdout, ["rclcpp", "brew_only"])
    assert result.apt == ["ros-jazzy-rclcpp"]
    assert result.unresolved == ["brew_only"]
