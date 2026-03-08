"""ros2 launch-plus verb implementation.

Provides `ros2 launch-plus <package> <launcher>` command.
"""

from argparse import ArgumentParser
from typing import Any

try:
    from ros2cli.command import CommandExtension
except ImportError:
    # ros2cli not available (not in ROS 2 environment)
    class CommandExtension:  # type: ignore[no-redef]
        """Stub for when ros2cli is not available."""

        pass


class LaunchPlusCommand(CommandExtension):
    """ros2 launch-plus command extension."""

    def add_arguments(self, parser: ArgumentParser, cli_name: str) -> None:
        """Add command arguments."""
        parser.add_argument(
            "package",
            help="Package containing the launch file",
        )
        parser.add_argument(
            "launcher",
            help="Launch file name",
        )
        parser.add_argument(
            "--resolve",
            action="store_true",
            help="Only resolve launch file (no build or run)",
        )
        parser.add_argument(
            "--build",
            action="store_true",
            help="Only build (no run)",
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="Verbose output",
        )

    def main(self, *, args: Any) -> int:
        """Execute the command."""
        package = args.package
        launcher = args.launcher

        if args.resolve:
            print(f"Resolving {package}/{launcher}")
            print("Not yet implemented")
        elif args.build:
            print(f"Building {package}/{launcher}")
            print("Not yet implemented")
        else:
            print(f"Running {package}/{launcher}")
            print("Not yet implemented")

        return 0
