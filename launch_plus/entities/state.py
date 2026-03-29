"""Resolver state: shared mutable state, logging, and common exceptions.

This module is the foundation layer with no imports from ``resolver.py``,
breaking the circular dependency chain.  Both ``resolver.py`` and the
entity/action files import from here.
"""

from __future__ import annotations

from typing import Any

# ─── Exception ────────────────────────────────────────────────────────────────


class _PackageNotFetchedError(RuntimeError):
    """Deprecated — no longer raised.

    Previously used as a control flow exception for package fetching.
    Now ``_ensure_fetched()`` handles inline fetching and callers log errors
    + return fallbacks on failure.  Retained for backward compatibility.
    """

    def __init__(self, pkg_name: str):
        super().__init__(f"package '{pkg_name}' source not on disk; fetch failed")
        self.pkg_name = pkg_name


# ─── Resolver State ──────────────────────────────────────────────────────────


class ResolverState:
    """Bundles all mutable module-level resolver state.

    A single ``_state`` instance is created at module level.  All resolver
    functions access state through ``_state.X`` instead of bare globals.
    The test fixture resets state by calling ``_state.reset()``.
    """

    __slots__ = (
        "tracked",
        "declared_arg_names",
        "namespace_stack",
        "include_chain",
        "env",
        "inline_params",
        "global_params",
        "global_remaps",
        "global_param_files",
        "package_shares",
        "apply_opaque_file_access",
        "preview_mode",
        "lockfile_data",
        "rosdep_fallback",
        "apply_arg_defaults",
        "global_arg_cascade",
        "allow_unportable_paths",
        "fetch_dir",
        "fetched_packages",
        "rosdep_attempted",
        "root_source_key",
        "walk_depth",
    )

    def __init__(self) -> None:
        self.reset()

    def warn(self, msg: str) -> None:
        """Append a warning message."""
        self.tracked["warnings"].append(msg)

    def error(self, msg: str) -> None:
        """Append an error message."""
        self.tracked["errors"].append(msg)

    def reset(self) -> None:
        """Reset all state to initial values."""
        self.tracked: dict[str, Any] = {
            "packages": [],
            "includes": [],
            "nodes": [],
            "warnings": [],
            "errors": [],
            "declared_args": [],
            "declared_args_by_file": {},
            "global_params": [],
            "include_args": {},
            "param_files": [],
            "set_launch_configurations": {},
            "include_deps": [],
            "param_file_deps": [],
            "event_handlers": [],
        }
        self.declared_arg_names: set = set()
        self.namespace_stack: list = []
        self.include_chain: list = []
        self.env: dict = {}
        self.inline_params: bool = False
        self.global_params: list[tuple[str, Any]] = []
        self.global_remaps: list[tuple[str, str]] = []
        self.global_param_files: list[dict] = []
        self.package_shares: dict = {}
        self.apply_opaque_file_access: bool = False
        self.preview_mode: bool = True
        self.lockfile_data: dict = {}
        self.rosdep_fallback: bool = False
        self.apply_arg_defaults: bool = False
        self.global_arg_cascade: bool = False
        self.allow_unportable_paths: bool = False
        self.fetch_dir: str = ""
        self.fetched_packages: set = set()
        self.rosdep_attempted: set = set()
        self.root_source_key: str = ""
        self.walk_depth: int = 0


_state = ResolverState()


# ─── Logging ──────────────────────────────────────────────────────────────────


def _warn(msg: str) -> None:
    _state.warn(msg)


def _error(msg: str) -> None:
    _state.error(msg)


# ─── LaunchContext stub ───────────────────────────────────────────────────────


class _StubLaunchContext:
    """Minimal LaunchContext: holds launch_configurations for substitution resolution."""

    def __init__(self):
        self._launch_configurations = {}

    @property
    def launch_configurations(self):
        return self._launch_configurations

    @launch_configurations.setter
    def launch_configurations(self, value):
        self._launch_configurations = value

    def perform_substitution(self, sub):
        if hasattr(sub, "perform"):
            return sub.perform(self)
        return str(sub)
