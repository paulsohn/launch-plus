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

    # ─── Package tracking and resolution ──────────────────────────────────

    def track_package(self, pkg) -> None:
        """Record a package reference for dependency tracking."""
        if not pkg:
            return
        if hasattr(pkg, "perform"):
            return  # Skip unresolved substitution objects
        pkg = str(pkg)
        if pkg and not pkg.startswith("$(") and pkg not in self.tracked["packages"]:
            self.tracked["packages"].append(pkg)

    def current_source_key(self) -> str:
        """Return the source key for the current file being resolved."""
        if self.include_chain:
            pkg, path = self.include_chain[-1]
            return f"{pkg}://{path}" if pkg else path
        return str(self.root_source_key)

    def track_node(self, node_dict: dict) -> int:
        """Append a node dict to tracked["nodes"] with source info from include_chain."""
        if self.include_chain:
            node_dict["include_chain"] = list(self.include_chain)
        idx = len(self.tracked["nodes"])
        self.tracked["nodes"].append(node_dict)
        return idx

    def track_event_handler(self, eh_dict: dict) -> int:
        """Track an event handler as a node-shaped entry for encounter ordering."""
        node_dict = {
            "package": "",
            "executable": "",
            "name": "",
            "ros_namespace": eh_dict.get("ros_namespace"),
            "explicit_namespace": eh_dict.get("explicit_namespace"),
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "event_handler",
            "plugins": [],
            "target": eh_dict.get("target"),
            "handler_kind": eh_dict.get("handler_kind", ""),
            "target_node": eh_dict.get("target_node"),
            "start_state": eh_dict.get("start_state"),
            "goal_state": eh_dict.get("goal_state"),
            "eh_actions": eh_dict.get("actions", []),
        }
        return self.track_node(node_dict)

    def track_include(self, path, *, ros_namespace=None) -> int:
        """Record an included file for dependency tracking. Returns dep index."""
        from launch_plus.entities.helpers import _extract_pkg_and_share_path

        if not path:
            return -1
        path = str(path)
        if path not in self.tracked["includes"]:
            self.tracked["includes"].append(path)
        dep = _extract_pkg_and_share_path(path)
        if dep:
            entry = {
                "package": dep[0],
                "share_path": dep[1],
                "path": path,
                "ros_namespace": ros_namespace,
                "include_args": {},
            }
            self.tracked["include_deps"].append(entry)
        for i in range(len(self.tracked["include_deps"]) - 1, -1, -1):
            if self.tracked["include_deps"][i].get("path") == path:
                return i
        return -1

    def track_param_file(self, path) -> None:
        """Record a parameter file for dependency tracking."""
        from launch_plus.entities.helpers import _extract_pkg_and_share_path

        if not path:
            return
        path = str(path)
        if path not in self.tracked["param_files"]:
            self.tracked["param_files"].append(path)
        dep = _extract_pkg_and_share_path(path)
        if dep:
            entry = {"package": dep[0], "share_path": dep[1]}
            if entry not in self.tracked["param_file_deps"]:
                self.tracked["param_file_deps"].append(entry)

    def record_declared_arg(self, name: str, default: str, *, flat: bool = True) -> None:
        """Record a declared arg in the per-file dict and optionally the flat list."""
        if flat:
            self.tracked["declared_args"].append({"name": name, "default": default})
        key = self.current_source_key()
        if key:
            by_file = self.tracked["declared_args_by_file"]
            if key not in by_file:
                by_file[key] = []
            by_file[key].append({"name": name, "default": default})

    def track_node_from_action(self, package, executable, name=None) -> int:
        """Track a node from an unpatched ROS 2 action (e.g. OpaqueFunction return)."""
        self.track_package(package)
        package = str(package) if package else ""
        executable = str(executable) if executable else ""
        name = str(name) if name else ""
        return self.track_node(
            {
                "package": package,
                "executable": executable,
                "name": name,
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "node",
                "plugins": [],
                "target": None,
            }
        )

    # ─── Package resolution ───────────────────────────────────────────────

    def resolve_pkg_share(self, package: str) -> str:
        """Resolve a package share directory path.

        Preview mode + lockfile: source directory (fetch inline if needed).
        Preview mode + no lockfile: AMENT_PREFIX_PATH, else portable fallback.
        Non-preview mode: AMENT_PREFIX_PATH only.

        Returns absolute path or portable syntax ``$(find-pkg-share pkg)``.
        Raises ``LookupError`` in non-preview mode if package not found.
        """
        # 1. Fast path: cached in package_shares.
        if package in self.package_shares:
            pkg_path: str = self.package_shares[package]
            # In preview mode, lockfile packages may need full fetch.
            if (
                self.preview_mode
                and self.lockfile_data
                and package in self.lockfile_data
                and not os.path.isfile(os.path.join(pkg_path, "package.xml"))
            ):
                if self._ensure_fetched(package):
                    return str(self.package_shares[package])
                logger.error("failed to fetch package '%s' from lockfile", package)
                return f"$(find-pkg-share {package})"
            return pkg_path

        # 2. Lockfile package not yet cached — fetch (preview only).
        if self.preview_mode and self.lockfile_data and package in self.lockfile_data:
            if self._ensure_fetched(package):
                return str(self.package_shares[package])
            logger.error("failed to fetch package '%s' from lockfile", package)
            return f"$(find-pkg-share {package})"

        # 3. Non-lockfile packages: use AMENT_PREFIX_PATH.
        real_gps = _get_real_get_package_share_directory()
        if real_gps is not None:
            try:
                return str(real_gps(package))
            except Exception:
                pass

        # 4. Try rosdep install if enabled.
        if self.rosdep_fallback and self._try_rosdep_install(package) and real_gps is not None:
            try:
                return str(real_gps(package))
            except Exception:
                pass

        # 5. Fallback.
        if self.preview_mode:
            return f"$(find-pkg-share {package})"
        raise LookupError(f"package '{package}' not found in AMENT_PREFIX_PATH")

    def _ensure_fetched(self, package: str) -> bool:
        """Ensure a lockfile package is fully fetched (has package.xml on disk).

        Performs inline git sparse-checkout if needed.  Updates
        ``self.package_shares`` after successful fetch.
        """
        import subprocess

        if package in self.fetched_packages:
            return True

        pkg_info = self.lockfile_data.get(package)
        if not pkg_info or not self.fetch_dir:
            return False

        repo_workspace_path = pkg_info["repo"]
        pkg_path_in_repo = pkg_info["path"]
        repo_url = pkg_info["url"]
        repo_sha = pkg_info["version"]

        repo_dir = os.path.join(self.fetch_dir, repo_workspace_path)
        pkg_dir = os.path.join(repo_dir, pkg_path_in_repo)

        # Already fully fetched?
        if os.path.isfile(os.path.join(pkg_dir, "package.xml")):
            self.fetched_packages.add(package)
            self.package_shares[package] = pkg_dir
            return True

        # Fetch via git sparse-checkout.
        try:
            sparse_path = "/**" if pkg_path_in_repo in (".", "") else pkg_path_in_repo
            if not os.path.isdir(os.path.join(repo_dir, ".git")):
                os.makedirs(repo_dir, exist_ok=True)
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "--filter=blob:none",
                        "--sparse",
                        "--single-branch",
                        "--depth=1",
                        repo_url,
                        repo_dir,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "sparse-checkout", "set", "--no-cone", sparse_path],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "checkout", repo_sha],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                subprocess.run(
                    ["git", "sparse-checkout", "add", sparse_path],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            if os.path.isfile(os.path.join(pkg_dir, "package.xml")):
                self.fetched_packages.add(package)
                self.package_shares[package] = pkg_dir
                return True
            logger.warning("fetched '%s' but package.xml not found at %s", package, pkg_dir)
            return False
        except subprocess.CalledProcessError as e:
            logger.warning("git fetch failed for '%s': %s", package, e.stderr or e)
            return False
        except Exception as e:
            logger.warning("failed to fetch '%s': %s", package, e)
            return False

    def _try_rosdep_install(self, package: str) -> bool:
        """Try installing a package via rosdep (apt backend)."""
        import subprocess

        if package in self.rosdep_attempted:
            return False
        self.rosdep_attempted.add(package)
        ros_distro = os.environ.get("ROS_DISTRO", "")
        if not ros_distro:
            return False
        try:
            resolve = subprocess.run(
                ["rosdep", "resolve", "--rosdistro", ros_distro, package],
                capture_output=True,
                text=True,
            )
            if resolve.returncode != 0:
                return False
            apt_pkgs = _parse_rosdep_resolve(resolve.stdout)
            if not apt_pkgs:
                return False
            install = subprocess.run(
                ["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", *apt_pkgs],
                capture_output=True,
                text=True,
            )
            return install.returncode == 0
        except Exception:
            return False


def _parse_rosdep_resolve(stdout: str) -> list[str]:
    """Parse ``rosdep resolve`` stdout into a list of apt package names."""
    apt_pkgs: list[str] = []
    installer = ""
    for line in stdout.strip().splitlines():
        if line.startswith("#"):
            installer = line.lstrip("#").strip()
        elif installer == "apt" and line.strip():
            apt_pkgs.extend(line.strip().split())
    return apt_pkgs


def _get_real_get_package_share_directory():
    """Lazy-import ament_index_python for package share directory lookup."""
    try:
        from ament_index_python.packages import get_package_share_directory

        return get_package_share_directory
    except Exception:
        return None


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

    def perform_substitutions(self, subs: list) -> str:
        """Resolve a list of substitutions to a string.

        Matches official ``perform_substitutions(context, subs)`` exactly.
        """
        return "".join([self.perform_substitution(sub) for sub in subs])

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

    def perform_substitution_ex(self, sub) -> tuple[str | None, bool]:
        """Resolve with fallback tracking. Returns (value, is_fallback).

        *is_fallback* is True when perform() returned None or raised and the
        display name was used instead.
        """
        if sub is None:
            return None, False
        if isinstance(sub, str):
            return sub, False
        if isinstance(sub, (list, tuple)):
            parts = []
            any_fallback = False
            for s in sub:
                try:
                    result = s.perform(self) if not isinstance(s, str) else s
                    if result is not None:
                        parts.append(str(result))
                    else:
                        parts.append(str(s))
                        any_fallback = True
                except Exception:
                    parts.append(str(s))
                    any_fallback = True
            return "".join(parts), any_fallback
        try:
            result = sub.perform(self)
            if result is None:
                return str(sub), True
            return str(result), False
        except Exception:
            return str(sub), True


# Backward compat aliases
_StubLaunchContext = LaunchContext
_SubstitutionContext = LaunchContext
