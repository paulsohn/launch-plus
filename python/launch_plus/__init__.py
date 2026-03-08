"""launch-plus: Bazel-like build and run system for ROS 2.

This package provides a Bazel-inspired workflow for ROS 2 that enables
lazy, on-demand package fetching and building based on actual launch-time
dependencies.

Usage:
    # Standalone CLI
    launch-plus run <package> <launcher>

    # ros2 verb
    ros2 launch-plus <package> <launcher>

    # Python API
    from launch_plus import index, resolve, build, run
"""

__version__ = "0.1.0"

# Import from Rust core when available
try:
    from launch_plus._core import hello as _hello

    def hello() -> str:
        """Test function to verify Rust bindings work."""
        return _hello()

except ImportError:
    # Rust extension not built yet
    def hello() -> str:
        """Test function (Rust bindings not available)."""
        return "Hello from launch-plus (Python-only mode)"


__all__ = [
    "__version__",
    "hello",
]
