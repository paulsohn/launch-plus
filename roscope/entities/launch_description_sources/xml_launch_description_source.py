"""XMLLaunchDescriptionSource — deferred XML launch file location resolution.

Matches official ``launch_xml.launch_description_sources.XMLLaunchDescriptionSource``.
"""

from __future__ import annotations

from roscope.entities.launch_description_sources.frontend_launch_description_source import (
    FrontendLaunchDescriptionSource,
)
from roscope.parsers.xml_parser import parse_xml_launch


class XMLLaunchDescriptionSource(FrontendLaunchDescriptionSource):
    """Encapsulation of an XML launch file, which can be loaded during launch."""

    def __init__(
        self,
        launch_file_path,
    ) -> None:
        """
        Create an XMLLaunchDescriptionSource.

        The given file path should be to a ``.launch.xml`` style file.

        :param launch_file_path: the path to the launch file. It can be made up of Substitution
            instances which are expanded when the location is resolved.
        """
        super().__init__(
            launch_file_path,
            method="interpreted XML launch file",
            parser=parse_xml_launch,
        )
