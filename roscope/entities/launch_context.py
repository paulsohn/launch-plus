# Copyright 2018 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Originally from:
# - https://github.com/ros2/launch/blob/rolling/launch/launch/launch_context.py
# Modified for roscope project by Taeseung Sohn, 2026.

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

logger = logging.getLogger("roscope")

# ─── Resolver State ──────────────────────────────────────────────────────────


class ResolverState:
    """Bundles resolver-specific mutable state.

    This is NOT part of the launch context — it holds resolver infrastructure
    (tracking, fetching, options) that the official ``LaunchContext`` does not
    have.  Entity modules receive state via ``context._state``.
    """

    __slots__ = (
        "packages",
        "include_deps",
        "param_file_deps",
        "declared_arg_names",
        "declared_arg_names_by_file",
        "include_chain",
        "package_shares",
        "preview_mode",
        "lockfile_data",
        "rosdep_fallback",
        "fetch_dir",
        "fetch_options",
        "fetched_packages",
        "rosdep_attempted",
        "root_source_key",
        "show_empty_includes",
        "show_args",
        "connection_plugin",
        "connection_registry",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Reset all state to initial values."""
        self.packages: list[str] = []
        self.include_deps: list[dict] = []
        self.param_file_deps: list[dict] = []
        self.declared_arg_names: set = set()
        self.declared_arg_names_by_file: dict[str, dict] = {}
        self.include_chain: list = []
        self.package_shares: dict = {}
        self.preview_mode: bool = True
        self.lockfile_data: dict = {}
        self.rosdep_fallback: bool = False
        self.fetch_dir: str = ""
        self.fetch_options: Any = None  # FetchOptions from fetcher.py
        self.fetched_packages: set = set()
        self.rosdep_attempted: set = set()
        self.root_source_key: str = ""
        self.show_empty_includes: bool = False
        self.show_args: bool = False
        self.connection_plugin = None
        # resolved_to -> [(package, executable, connection_type), ...]
        self.connection_registry: dict[str, list[tuple[str, str, str]]] = {}

    # ─── Package tracking and resolution ──────────────────────────────────

    def track_package(self, pkg) -> None:
        """Record a package reference for dependency tracking."""
        if not pkg:
            return
        # Skip unresolved substitution objects (single or list)
        from roscope.entities.substitution import Substitution

        if isinstance(pkg, Substitution):
            return
        if isinstance(pkg, (list, tuple)) and any(isinstance(i, Substitution) for i in pkg):
            return
        pkg = str(pkg)
        if pkg and pkg not in self.packages:
            self.packages.append(pkg)

    def current_source_key(self) -> str:
        """Return the source key (file path) for the current file being resolved."""
        if self.include_chain:
            return str(self.include_chain[-1])
        return str(self.root_source_key)

    def track_include(self, path, *, ros_namespace=None) -> int:
        """Record an included file for dependency tracking. Returns dep index."""
        from roscope.entities.helpers import _extract_pkg_and_share_path

        if not path:
            return -1
        path = str(path)
        dep = _extract_pkg_and_share_path(path)
        if dep:
            entry = {
                "package": dep[0],
                "share_path": dep[1],
                "path": path,
                "ros_namespace": ros_namespace,
                "include_args": {},
            }
            self.include_deps.append(entry)
        for i in range(len(self.include_deps) - 1, -1, -1):
            if self.include_deps[i].get("path") == path:
                return i
        return -1

    def track_param_file(self, path) -> None:
        """Record a parameter file for dependency tracking."""
        from roscope.entities.helpers import _extract_pkg_and_share_path

        if not path:
            return
        path = str(path)
        dep = _extract_pkg_and_share_path(path)
        if dep:
            entry = {"package": dep[0], "share_path": dep[1]}
            if entry not in self.param_file_deps:
                self.param_file_deps.append(entry)

    # ─── Package resolution ───────────────────────────────────────────────

    def resolve_pkg_share(self, package: str) -> str:
        """Resolve a package share directory path.

        Delegates to ``fetcher.ensure_package_available()`` for the unified
        lockfile → AMENT → rosdep resolution. Caches results in
        ``package_shares``.

        In preview mode, returns the source directory path.
        In non-preview mode, returns the installed path from AMENT_PREFIX_PATH.

        Returns an absolute filesystem path.
        Raises ``LookupError`` if the package cannot be found.
        """
        if package in self.package_shares:
            return str(self.package_shares[package])

        from pathlib import Path

        from roscope.fetcher import ensure_package_available

        # In post-build mode, source paths are not authoritative — only the
        # installed workspace (AMENT_PREFIX_PATH) is.  Pass lockfile=None to
        # ensure_package_available so it skips the lockfile/source lookup and
        # goes straight to AMENT_PREFIX_PATH.
        if self.preview_mode:
            lockfile = self._build_lockfile() if self.lockfile_data else None
        else:
            lockfile = None
        fetch_dir = Path(self.fetch_dir)
        result = ensure_package_available(
            package,
            lockfile,
            fetch_dir,
            self.fetch_options,
            rosdep_fallback=self.rosdep_fallback,
        )
        if result is not None:
            resolved = str(result)
            self.package_shares[package] = resolved
            self.fetched_packages.add(package)
            return resolved

        raise LookupError(f"package '{package}' not found")

    def apply_connection_plugin(
        self,
        pkg: str,
        params: dict,
        remaps: list,
        *,
        executable: str | None = None,
        plugin_name: str | None = None,
    ) -> dict[str, dict]:
        """Call the connection plugin, extend remaps in-place, register connections.

        Exactly one of *executable* or *plugin_name* must be provided.

        Returns the validated remap_metadata dict (keyed by 'from' / connection name).
        Returns {} when no plugin is set, the package cannot be resolved, or the
        plugin degrades gracefully.
        """
        if self.connection_plugin is None or not pkg:
            return {}
        pkg_share: str | None = None
        try:
            pkg_share = self.resolve_pkg_share(pkg)
        except Exception:
            return {}
        from roscope.connection_plugin import call_plugin

        meta_by_connection = call_plugin(
            self.connection_plugin,
            pkg_share,
            params,
            executable=executable,
            plugin_name=plugin_name,
        )
        if not meta_by_connection:
            return {}
        identifier = executable or plugin_name or ""
        remap_metadata: dict[str, dict] = {}
        existing_froms = {r[0] for r in remaps if isinstance(r, (list, tuple)) and len(r) >= 2}
        for conn, meta in meta_by_connection.items():
            remap_metadata[conn] = meta
            if conn not in existing_froms:
                remaps.append([conn, conn])
        remap_to = {r[0]: r[1] for r in remaps if isinstance(r, (list, tuple)) and len(r) >= 2}
        for conn, meta in remap_metadata.items():
            conn_type = meta.get("type")
            if conn_type:
                self.register_connection(remap_to.get(conn, conn), conn_type, pkg, identifier)
        return remap_metadata

    def register_connection(self, resolved_to: str, conn_type: str, pkg: str, exe: str) -> None:
        """Record a typed connection and log an error if a protocol-family conflict arises."""
        from roscope.connection_plugin import PROTOCOL_FAMILY

        family = PROTOCOL_FAMILY.get(conn_type)
        if family is None:
            return
        entries = self.connection_registry.setdefault(resolved_to, [])
        entries.append((pkg, exe, conn_type))
        families = {PROTOCOL_FAMILY[t] for _, _, t in entries}
        if len(families) > 1:
            detail = ", ".join(f"{p}/{e}:{t}" for p, e, t in entries)
            logger.error(
                "connection type conflict on %r: mixed protocol families (%s) — %s",
                resolved_to,
                ", ".join(sorted(families)),
                detail,
            )

    def _build_lockfile(self):
        """Reconstruct a minimal Lockfile from lockfile_data for fetcher API."""
        from roscope.types import Lockfile, PackageLock, RepoLock

        packages = {}
        repositories = {}
        for pkg_name, info in self.lockfile_data.items():
            packages[pkg_name] = PackageLock(repo=info["repo"], path=info["path"])
            if info["repo"] not in repositories:
                repositories[info["repo"]] = RepoLock(url=info["url"], version=info["version"])
        return Lockfile(packages=packages, repositories=repositories)


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
            state = ResolverState()
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

    def perform_substitutions(self, subs: list) -> str:
        """Resolve a list of substitutions to a string.

        Convenience wrapper around ``perform_substitutions(context, subs)``.
        """
        from roscope.entities.utilities import perform_substitutions

        return perform_substitutions(self, subs)

    def perform_substitution(self, sub) -> str:
        """Resolve a single substitution to a string.

        Matches official ``context.perform_substitution(sub)`` API.
        Also handles ``str`` (returned as-is), ``None`` (returns ""),
        and ``list[Substitution]`` (delegates to ``perform_substitutions``).
        """
        if sub is None:
            return ""
        if isinstance(sub, str):
            return sub
        if isinstance(sub, (list, tuple)):
            return self.perform_substitutions(list(sub))
        try:
            result = sub.perform(self)
            return str(result) if result is not None else str(sub)
        except Exception:
            return str(sub)
