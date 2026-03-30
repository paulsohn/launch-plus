"""Resolver state: shared mutable state and common exceptions.

This module defines ``ResolverState`` (the mutable state bundle) and
``_StubLaunchContext`` (minimal launch context for substitution resolution).
The canonical ``_state`` instance lives in ``resolver.py``; entity modules
receive state via function/method parameters and never import a global
singleton from this module.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("launch_plus")


class _TrackedLogHandler(logging.Handler):
    """Handler that appends log messages to ``tracked["warnings"]``/``tracked["errors"]``.

    Attached to the ``launch_plus`` logger for the lifetime of a
    :class:`ResolverState` so that ``logger.warning()``/``logger.error()``
    calls automatically populate the tracked diagnostic lists.
    """

    def __init__(self, tracked: dict[str, Any]) -> None:
        super().__init__()
        self.tracked = tracked

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        if record.levelno >= logging.ERROR:
            self.tracked["errors"].append(msg)
        elif record.levelno >= logging.WARNING:
            self.tracked["warnings"].append(msg)


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
        "_log_handler",
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
        self._log_handler: _TrackedLogHandler | None = None
        self.reset()

    def warn(self, msg: str) -> None:
        """Log a warning via the project logger."""
        _logger.warning("%s", msg)

    def error(self, msg: str) -> None:
        """Log an error via the project logger."""
        _logger.error("%s", msg)

    def reset(self) -> None:
        """Reset all state to initial values."""
        # Detach previous log handler if any.
        if hasattr(self, "_log_handler") and self._log_handler is not None:
            _logger.removeHandler(self._log_handler)
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
        # Attach a log handler that populates tracked["warnings"]/["errors"].
        self._log_handler = _TrackedLogHandler(self.tracked)
        _logger.addHandler(self._log_handler)


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
