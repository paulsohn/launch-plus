"""Tests for roscope.locator — ported from locator.rs Rust tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from roscope.locator import MultipleLaunchFilesError, PackageLocator, find_share_file
from roscope.types import Lockfile, PackageLock, RepoLock


def _mock_lockfile() -> Lockfile:
    return Lockfile(
        repositories={
            "autoware/launcher/autoware_launch": RepoLock(
                url="https://github.com/autowarefoundation/autoware_launch.git",
                version="abc123",
                version_ref="main",
                packages=["autoware_launch"],
            ),
        },
        packages={
            "autoware_launch": PackageLock(
                repo="autoware/launcher/autoware_launch",
                path="autoware_launch",
            ),
        },
    )


# =========================================================================
# Lockfile mode tests (resolve_*)
# =========================================================================


def test_resolve_package_share() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = _mock_lockfile()
        locator = PackageLocator.from_lockfile(tmp, lockfile)
        result = locator.resolve_package_share("autoware_launch")
        assert result is not None
        expected = Path(tmp) / "autoware/launcher/autoware_launch" / "autoware_launch"
        assert result == expected


def test_resolve_package_share_not_in_lockfile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = _mock_lockfile()
        locator = PackageLocator.from_lockfile(tmp, lockfile)
        assert locator.resolve_package_share("nonexistent_pkg") is None


def test_resolve_launch_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = _mock_lockfile()
        locator = PackageLocator.from_lockfile(tmp, lockfile)
        result = locator.resolve_launch_file("autoware_launch", "autoware.launch.xml")
        assert result is not None
        expected = (
            Path(tmp)
            / "autoware/launcher/autoware_launch"
            / "autoware_launch"
            / "launch"
            / "autoware.launch.xml"
        )
        assert result == expected


def test_resolve_share_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = _mock_lockfile()
        locator = PackageLocator.from_lockfile(tmp, lockfile)
        result = locator.resolve_share_file("autoware_launch", Path("config/vehicle.yaml"))
        assert result is not None
        expected = (
            Path(tmp)
            / "autoware/launcher/autoware_launch"
            / "autoware_launch"
            / "config"
            / "vehicle.yaml"
        )
        assert result == expected


def test_has_package() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = _mock_lockfile()
        locator = PackageLocator.from_lockfile(tmp, lockfile)
        assert locator.has_package("autoware_launch")
        assert not locator.has_package("nonexistent")


def test_get_package_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = _mock_lockfile()
        locator = PackageLocator.from_lockfile(tmp, lockfile)
        pkg_lock = locator.get_package_lock("autoware_launch")
        assert pkg_lock is not None
        assert pkg_lock.path == "autoware_launch"


# =========================================================================
# Discovery mode tests (locate_*)
# =========================================================================


def test_locate_package_simple_layout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pkg_dir = Path(tmp) / "my_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "package.xml").write_text("<package></package>")
        locator = PackageLocator.with_workspace(tmp)
        result = locator.locate_package_share("my_pkg")
        assert result is not None
        assert result == pkg_dir


def test_locate_package_python_layout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pkg_dir = Path(tmp) / "my_pkg" / "my_pkg"
        pkg_dir.mkdir(parents=True)
        locator = PackageLocator.with_workspace(tmp)
        result = locator.locate_package_share("my_pkg")
        assert result is not None
        assert result == pkg_dir


def test_locate_launch_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pkg_dir = Path(tmp) / "my_pkg"
        launch_dir = pkg_dir / "launch"
        launch_dir.mkdir(parents=True)
        (pkg_dir / "package.xml").write_text("<package></package>")
        (launch_dir / "test.launch.xml").write_text("<launch></launch>")
        locator = PackageLocator.with_workspace(tmp)
        result = locator.locate_launch_file("my_pkg", "test.launch.xml")
        assert result is not None
        assert result == launch_dir / "test.launch.xml"


def test_locate_launch_file_not_found() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        locator = PackageLocator.with_workspace(tmp)
        assert locator.locate_launch_file("nonexistent", "test.launch.xml") is None


def test_locate_launch_file_in_subdirectory() -> None:
    """A launch file nested under a subdirectory of launch/ is still found.

    Matches upstream ros2launch, which searches the whole package share
    directory rather than only ``launch/`` directly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pkg_dir = Path(tmp) / "my_pkg"
        nested_dir = pkg_dir / "launch" / "components"
        nested_dir.mkdir(parents=True)
        (pkg_dir / "package.xml").write_text("<package></package>")
        (nested_dir / "test.launch.xml").write_text("<launch></launch>")
        locator = PackageLocator.with_workspace(tmp)
        result = locator.locate_launch_file("my_pkg", "test.launch.xml")
        assert result == nested_dir / "test.launch.xml"


def test_find_share_file_multiple_matches_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        share_dir = Path(tmp)
        (share_dir / "a").mkdir()
        (share_dir / "b").mkdir()
        (share_dir / "a" / "dup.launch.xml").write_text("<launch></launch>")
        (share_dir / "b" / "dup.launch.xml").write_text("<launch></launch>")
        with pytest.raises(MultipleLaunchFilesError):
            find_share_file(share_dir, "dup.launch.xml")


def test_find_share_file_not_found_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(FileNotFoundError):
        find_share_file(Path(tmp), "missing.launch.xml")
