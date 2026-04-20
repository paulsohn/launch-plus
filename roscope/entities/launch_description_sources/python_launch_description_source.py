"""PythonLaunchDescriptionSource — deferred Python launch file location resolution.

Matches official ``launch.launch_description_sources.PythonLaunchDescriptionSource``.
"""

from __future__ import annotations

from roscope.entities.launch_description_source import LaunchDescriptionSource


class PythonLaunchDescriptionSource(LaunchDescriptionSource):
    """Encapsulation of a Python launch file, which can be loaded during launch."""

    def __init__(
        self,
        launch_file_path,
    ) -> None:
        """
        Create a PythonLaunchDescriptionSource.

        The given file path should be to a ``.launch.py`` style file.

        :param launch_file_path: the path to the launch file. It can be made up of Substitution
            instances which are expanded when the location is resolved.
        """
        super().__init__(launch_file_path, "interpreted python launch file")
