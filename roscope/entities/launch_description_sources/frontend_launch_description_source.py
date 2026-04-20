"""FrontendLaunchDescriptionSource — deferred declarative launch file location resolution.

Matches official ``launch.launch_description_sources.FrontendLaunchDescriptionSource``.
"""

from __future__ import annotations

from roscope.entities.launch_description_source import LaunchDescriptionSource


class FrontendLaunchDescriptionSource(LaunchDescriptionSource):
    """Encapsulation of a declarative (markup-based) launch file."""

    def __init__(
        self,
        launch_file_path,
        *,
        method: str = "interpreted frontend launch file",
        parser=None,
    ) -> None:
        """
        Create a FrontendLaunchDescriptionSource.

        :param launch_file_path: the path to the launch file. It can be made up of Substitution
            instances which are expanded when the location is resolved.
        :param method: human-readable description of how the launch description is generated.
        :param parser: stored for API compatibility with the official
            ``FrontendLaunchDescriptionSource``. Not used for dispatch — the
            resolver selects a parser by file extension instead.
        """
        super().__init__(launch_file_path, method)
        self._parser = parser
