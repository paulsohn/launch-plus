"""Resolver state and launch context.

``ResolverState`` bundles resolver-specific mutable state (tracking, fetching,
options).  ``LaunchContext`` mirrors the official ROS 2 ``LaunchContext``
(globals, locals, launch_configurations, environment — each with push/pop
stacks).  Actions interact with ``LaunchContext``; the resolver manages
``ResolverState``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("launch_plus")

# ─── Resolver State ──────────────────────────────────────────────────────────


class ResolverState:
    """Bundles resolver-specific mutable state.

    This is NOT part of the launch context — it holds resolver infrastructure
    (tracking, fetching, options) that the official ``LaunchContext`` does not
    have.  Entity modules receive state via ``context._state``.
    """

    __slots__ = (
        "tracked",
        "declared_arg_names",
        "include_chain",
        "inline_params",
        "package_shares",
        "apply_opaque_file_access",
        "preview_mode",
        "lockfile_data",
        "rosdep_fallback",
        "apply_arg_defaults",
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
        self.include_chain: list = []
        self.inline_params: bool = False
        self.package_shares: dict = {}
        self.apply_opaque_file_access: bool = False
        self.preview_mode: bool = True
        self.lockfile_data: dict = {}
        self.rosdep_fallback: bool = False
        self.apply_arg_defaults: bool = False
        self.allow_unportable_paths: bool = False
        self.fetch_dir: str = ""
        self.fetched_packages: set = set()
        self.rosdep_attempted: set = set()
        self.root_source_key: str = ""
        self.walk_depth: int = 0


# ─── Launch Context ──────────────────────────────────────────────────────────


class LaunchContext:
    """Launch context matching the official ROS 2 ``LaunchContext`` structure.

    Provides the same scoping primitives as the official implementation:
    globals/locals (with stack), launch_configurations (with stack),
    environment (with stack), and ``perform_substitution()``.

    Event-related fields are omitted (we're a static resolver, not a runtime).
    ``_state`` is our resolver-specific extension for tracking and fetching.
    """

    def __init__(self, state: ResolverState | None = None):
        if state is None:
            from launch_plus.resolver import get_state

            state = get_state()
        self._state: ResolverState = state

        # Locals system (matching official __globals, __locals_stack, __locals)
        self._globals: dict[str, Any] = {}
        self._locals_stack: list[dict[str, Any]] = []
        self._locals: dict[str, Any] = {}

        # Launch configurations (matching official)
        self._launch_configurations: dict[str, Any] = {}
        self._launch_configurations_stack: list[dict[str, Any]] = []

        # Environment (matching official __environment_stack)
        # Initialized from os.environ, matching official LaunchContext.
        self._environment: dict[str, str] = dict(os.environ)
        self._environment_stack: list[dict[str, str]] = []

        # Resolver-specific fields (not in official LaunchContext)
        self.launch_file_dir: str | None = None
        self.preview_mode: bool = False

    # ─── Launch configurations ────────────────────────────────────────────

    @property
    def launch_configurations(self) -> dict[str, Any]:
        """Return the current launch configurations dict."""
        return self._launch_configurations

    @launch_configurations.setter
    def launch_configurations(self, value: dict[str, Any]) -> None:
        self._launch_configurations = value

    def _push_launch_configurations(self) -> None:
        """Save a snapshot of current launch_configurations onto the stack."""
        self._launch_configurations_stack.append(dict(self._launch_configurations))

    def _pop_launch_configurations(self) -> None:
        """Restore previously saved launch_configurations from the stack."""
        if not self._launch_configurations_stack:
            logger.error("launch_configurations stack unexpectedly empty")
            return
        self._launch_configurations = self._launch_configurations_stack.pop()

    # ─── Environment ──────────────────────────────────────────────────────

    @property
    def environment(self) -> dict[str, str]:
        """Return the current environment dict."""
        return self._environment

    def _push_environment(self) -> None:
        """Save a snapshot of current environment onto the stack."""
        self._environment_stack.append(dict(self._environment))

    def _pop_environment(self) -> None:
        """Restore previously saved environment from the stack."""
        if not self._environment_stack:
            logger.error("environment stack unexpectedly empty")
            return
        self._environment = self._environment_stack.pop()

    def _reset_environment(self) -> None:
        """Clear the environment (matching official ResetEnvironment)."""
        self._environment.clear()

    # ─── Locals ───────────────────────────────────────────────────────────

    def _push_locals(self) -> None:
        """Save current locals onto the stack."""
        self._locals_stack.append(dict(self._locals))

    def _pop_locals(self) -> None:
        """Restore previously saved locals from the stack."""
        if not self._locals_stack:
            logger.error("locals stack unexpectedly empty")
            return
        self._locals = self._locals_stack.pop()

    def extend_globals(self, extensions: dict[str, Any]) -> None:
        """Add key-value pairs to globals (permanent, overridable by locals)."""
        self._globals.update(extensions)

    def extend_locals(self, extensions: dict[str, Any]) -> None:
        """Add key-value pairs to current locals (until popped)."""
        self._locals.update(extensions)

    @property
    def locals(self) -> dict[str, Any]:
        """Return combined globals + locals (locals override globals)."""
        combined = dict(self._globals)
        combined.update(self._locals)
        return combined

    def get_locals_as_dict(self) -> dict[str, Any]:
        """Return locals as a dict (matching official API)."""
        return self.locals

    # ─── Substitution ─────────────────────────────────────────────────────

    def perform_substitution(self, sub) -> str | None:
        """Resolve a substitution to a string. Matches official ROS 2 API.

        Handles: None, str, list[Substitution], single .perform() object.
        """
        if sub is None:
            return None
        if isinstance(sub, str):
            return sub
        if isinstance(sub, (list, tuple)):
            parts = []
            for s in sub:
                if hasattr(s, "perform"):
                    try:
                        result = s.perform(self)
                        parts.append(str(result) if result is not None else str(s))
                    except Exception:
                        parts.append(str(s))
                else:
                    parts.append(str(s))
            return "".join(parts)
        if hasattr(sub, "perform"):
            try:
                result = sub.perform(self)
                return str(result) if result is not None else None
            except Exception:
                return str(sub)
        return str(sub)

    def perform_substitution_ex(self, sub) -> tuple[str | None, bool]:
        """Resolve with fallback tracking. Returns (value, is_fallback)."""
        if sub is None:
            return None, False
        if isinstance(sub, str):
            return sub, False
        if isinstance(sub, (list, tuple)):
            parts = []
            any_fallback = False
            for s in sub:
                if hasattr(s, "perform"):
                    try:
                        result = s.perform(self)
                        if result is not None:
                            parts.append(str(result))
                        else:
                            parts.append(str(s))
                            any_fallback = True
                    except Exception:
                        parts.append(str(s))
                        any_fallback = True
                else:
                    parts.append(str(s))
            return "".join(parts), any_fallback
        if hasattr(sub, "perform"):
            try:
                result = sub.perform(self)
                if result is None:
                    return str(sub), True
                return str(result), False
            except Exception:
                return str(sub), True
        return str(sub), True


# Backward compat aliases
_StubLaunchContext = LaunchContext
_SubstitutionContext = LaunchContext
