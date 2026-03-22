"""launch-plus: Bazel-like build and run system for ROS 2.

This package provides a Bazel-inspired workflow for ROS 2 that enables
lazy, on-demand package fetching and building based on actual launch-time
dependencies.

Usage:
    # Standalone CLI
    launch-plus resolve <package> <launcher>

    # ros2 verb
    ros2 launch-plus <package> <launcher>

    # Python API
    from launch_plus.types import ResolvedNode, ParsedLaunchFile
"""

__version__ = "0.1.0"

__all__ = [
    "__version__",
]
