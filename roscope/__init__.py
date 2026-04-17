"""roscope: Bazel-like build and run system for ROS 2.

This package provides a Bazel-inspired workflow for ROS 2 that enables
lazy, on-demand package fetching and building based on actual launch-time
dependencies.

Usage:
    # Standalone CLI
    roscope resolve <package> <launcher>

    # ros2 verb
    ros2 roscope <package> <launcher>

    # Python API
    from roscope.types import ParsedLaunchFile
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("roscope")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "__version__",
]
