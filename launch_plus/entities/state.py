"""Resolver state: shared mutable state and common exceptions.

This module defines ``ResolverState`` (the mutable state bundle) and
``_StubLaunchContext`` (minimal launch context for substitution resolution).
The canonical ``_state`` instance lives in ``resolver.py``; entity modules
receive state via function/method parameters and never import a global
singleton from this module.
"""

from __future__ import annotations

from typing import Any

# ─── Resolver State ──────────────────────────────────────────────────────────


class ResolverState:
    """Bundles all mutable resolver state.

    The entry point (``resolver.py``) owns the canonical ``_state`` instance.
    Entity modules receive state via function/method parameters — they never
    import a global singleton.  The test fixture resets state by calling
    ``_state.reset()``.
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

    def reset(self) -> None:
        """Reset all state to initial values."""
        self.tracked: dict[str, Any] = {
            "packages": [],
            "includes": [],
            "nodes": [],
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


# ─── Launch Context ──────────────────────────────────────────────────────────


class LaunchContext:
    """Unified launch context for both XML/YAML and Python resolution.

    Combines the roles of the former ``_SubstitutionContext`` (XML path)
    and ``_StubLaunchContext`` (Python shim path) into a single type
    that matches the official ROS 2 ``LaunchContext`` interface.

    Fields used by XML substitutions: ``args``, ``vars``, ``env``,
    ``launch_file_dir``, ``preview_mode``.

    Fields used by Python shim: ``_launch_configurations``.

    Both paths share: ``_state``, ``perform_substitution()``.
    """

    def __init__(self, state: ResolverState | None = None):
        if state is None:
            from launch_plus.resolver import get_state

            state = get_state()
        self._state: ResolverState = state
        # ROS 2 compat (Python shim path)
        self._launch_configurations: dict[str, object] = {}
        # XML/YAML substitution context
        self.args: dict[str, str] = {}
        self.vars: dict[str, str] = {}
        self.env: dict[str, str] = state.env  # shared reference
        self.launch_file_dir: str | None = None
        self.preview_mode: bool = False

    @property
    def launch_configurations(self) -> dict[str, object]:
        return self._launch_configurations

    @launch_configurations.setter
    def launch_configurations(self, value: dict[str, object]) -> None:
        self._launch_configurations = value

    def perform_substitution(self, sub) -> str:
        """Resolve a substitution object. Matches official ROS 2 API."""
        if hasattr(sub, "perform"):
            result = sub.perform(self)
            return str(result) if result is not None else ""
        return str(sub)


# Backward compat aliases
_StubLaunchContext = LaunchContext
_SubstitutionContext = LaunchContext
