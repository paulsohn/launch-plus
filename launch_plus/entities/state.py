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


# ─── LaunchContext stub ───────────────────────────────────────────────────────


class _StubLaunchContext:
    """Minimal LaunchContext: holds launch_configurations for substitution resolution.

    Carries a :class:`ResolverState` reference so that ``execute()`` methods
    can access resolver state via ``context._state``.  When *state* is not
    supplied, a lazy import fetches the canonical instance from ``resolver.py``.
    """

    def __init__(self, state: ResolverState | None = None):
        self._launch_configurations: dict[str, object] = {}
        if state is None:
            from launch_plus.resolver import get_state

            state = get_state()
        self._state = state

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
