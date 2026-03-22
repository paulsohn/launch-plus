"""ros2 CLI verb integration for launch-plus.

This module provides the `ros2 launch-plus` command by registering
as a ros2cli extension.
"""

from launch_plus.verb.launch_plus import LaunchPlusCommand

__all__ = ["LaunchPlusCommand"]
