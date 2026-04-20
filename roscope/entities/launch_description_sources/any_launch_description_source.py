"""AnyLaunchDescriptionSource — deferred launch file location resolution (any format).

Matches official ``launch.launch_description_sources.AnyLaunchDescriptionSource``.
"""

from __future__ import annotations

from roscope.entities.launch_description_source import LaunchDescriptionSource


class AnyLaunchDescriptionSource(LaunchDescriptionSource):
    """Encapsulation of a launch file of any supported format, which can be loaded during launch."""

    def __init__(
        self,
        launch_file_path,
    ) -> None:
        """
        Create an AnyLaunchDescriptionSource.

        :param launch_file_path: the path to the launch file. It can be made up of Substitution
            instances which are expanded when the location is resolved.
        """
        super().__init__(launch_file_path, "interpreted launch file")
