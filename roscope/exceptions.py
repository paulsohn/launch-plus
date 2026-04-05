"""Exception hierarchy for roscope.

Ported from crates/roscope-core/src/error.rs.
"""

from __future__ import annotations


class RoscopeError(Exception):
    """Base exception for all roscope errors."""


class YamlParseError(RoscopeError):
    """Failed to parse YAML file."""


class XmlParseError(RoscopeError):
    """Failed to parse XML file."""


class GitError(RoscopeError):
    """Git operation failed."""


class PackageNotFoundError(RoscopeError):
    """Package not found in lockfile."""


class RepoNotFoundError(RoscopeError):
    """Repository not found in lockfile."""


class DuplicatePackageError(RoscopeError):
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


class InvalidVersionError(RoscopeError):
    """Invalid version specification."""


class LaunchParseError(RoscopeError):
    """Launch file parsing error."""


class CircularDependencyError(RoscopeError):
    """Circular dependency detected."""


class PythonResolverError(RoscopeError):
    """Python resolver failed."""


class ProcessExecutionError(RoscopeError):
    """Process execution failed."""
