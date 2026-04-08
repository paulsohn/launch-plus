"""Tests for roscope.exceptions."""

from roscope.exceptions import (
    CircularDependencyError,
    DuplicatePackageError,
    GitError,
    InvalidVersionError,
    LaunchParseError,
    PackageNotFoundError,
    ProcessExecutionError,
    PythonResolverError,
    RepoNotFoundError,
    RoscopeError,
    XmlParseError,
    YamlParseError,
)


def test_all_inherit_from_base() -> None:
    """Every exception class is a subclass of RoscopeError."""
    subclasses = [
        YamlParseError,
        XmlParseError,
        GitError,
        PackageNotFoundError,
        RepoNotFoundError,
        DuplicatePackageError,
        InvalidVersionError,
        LaunchParseError,
        CircularDependencyError,
        PythonResolverError,
        ProcessExecutionError,
    ]
    for cls in subclasses:
        assert issubclass(cls, RoscopeError), f"{cls.__name__} not subclass of RoscopeError"


def test_duplicate_package_error_fields() -> None:
    err = DuplicatePackageError("my_pkg", "repo_a", "repo_b")
    assert err.package == "my_pkg"
    assert err.repo1 == "repo_a"
    assert err.repo2 == "repo_b"
    assert "my_pkg" in str(err)
    assert "repo_a" in str(err)
    assert "repo_b" in str(err)


def test_catch_as_base() -> None:
    """Can catch any roscope error via the base class."""
    try:
        raise GitError("clone failed")
    except RoscopeError as e:
        assert "clone failed" in str(e)
