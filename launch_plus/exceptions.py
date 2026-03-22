"""Exception hierarchy for launch-plus.

Ported from crates/launch-plus-core/src/error.rs.
"""

from __future__ import annotations


class LaunchPlusError(Exception):
    """Base exception for all launch-plus errors."""


class YamlParseError(LaunchPlusError):
    """Failed to parse YAML file."""


class XmlParseError(LaunchPlusError):
    """Failed to parse XML file."""


class GitError(LaunchPlusError):
    """Git operation failed."""


class PackageNotFoundError(LaunchPlusError):
    """Package not found in lockfile."""


class RepoNotFoundError(LaunchPlusError):
    """Repository not found in lockfile."""


class DuplicatePackageError(LaunchPlusError):
    """Duplicate package name across repositories.

    Attributes:
        package: The duplicate package name.
        repo1: First repository containing the package.
        repo2: Second repository containing the package.
    """

    def __init__(self, package: str, repo1: str, repo2: str) -> None:
        self.package = package
        self.repo1 = repo1
        self.repo2 = repo2
        super().__init__(f"duplicate package '{package}' found in repos: {repo1} and {repo2}")


class InvalidVersionError(LaunchPlusError):
    """Invalid version specification."""


class LaunchParseError(LaunchPlusError):
    """Launch file parsing error."""


class CircularDependencyError(LaunchPlusError):
    """Circular dependency detected."""


class PythonResolverError(LaunchPlusError):
    """Python resolver failed."""


class ProcessExecutionError(LaunchPlusError):
    """Process execution failed."""
