"""ros2 CLI verb integration for roscope.

This module provides the `ros2 roscope` command by registering
as a ros2cli extension.
"""

from roscope.verb.roscope import RoscopeCommand

__all__ = ["RoscopeCommand"]
