#!/usr/bin/env python3
"""
launch-plus resolver.

Resolves ROS 2 launch files (XML, YAML, and Python) by parsing their
structure, evaluating substitutions, and tracking nodes, includes, and
package dependencies.  Python launch files are handled via import-patching
hooks that intercept ``launch_ros`` and ``launch`` classes.

The main entry point is :func:`resolve_file`, which returns a
:class:`~launch_plus.types.ParsedLaunchFile`.
"""

import contextvars
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("launch_plus")

# Ensure substitution entity classes are registered before any parsing occurs.
import launch_plus.entities  # noqa: F401
from launch_plus.entities.state import (  # noqa: E402
    ResolverState,
    _StubLaunchContext,
)

# ─── Scoped state via contextvars ────────────────────────────────────────────
#
# resolve_file() creates a fresh ResolverState and sets _current_state for the
# duration of resolution.  All code that needs state uses get_state().
# This is thread-safe and supports concurrent resolution.

_current_state: contextvars.ContextVar[ResolverState] = contextvars.ContextVar(
    "_current_state",
)
# Set a default state for test/REPL use before resolve_file() is called.
_current_state.set(ResolverState())


def get_state() -> ResolverState:
    """Return the active ResolverState for the current execution context."""
    return _current_state.get()


# ─── Capture real ament_index_python BEFORE installing the import patcher ────
# This allows FindPackageShare.perform() to return real installed paths when
# the package is actually present in the build/install tree.

_real_get_package_share_directory = None
try:
    from ament_index_python.packages import get_package_share_directory as _real_gps

    _real_get_package_share_directory = _real_gps
except Exception:
    pass

# ─── ROS prefix fallback ─────────────────────────────────────────────────────
# Use $ROS_DISTRO env var so this works across distro versions (jazzy, rolling, etc.)
_ROS_DISTRO = os.environ.get("ROS_DISTRO", "")
_ROS_DISTRO_PREFIX = f"/opt/ros/{_ROS_DISTRO}" if _ROS_DISTRO else ""


def _parse_rosdep_resolve(stdout: str) -> list[str]:
    """Parse ``rosdep resolve`` stdout into a list of apt package names.

    Output format::

        #apt
        ros-humble-ublox-gps

    or with multiple packages on one line::

        #apt
        libnl-3-dev libnl-genl-3-dev

    Only ``#apt`` packages are returned; other installers (``#pip``, ``#brew``)
    are ignored.
    """
    apt_pkgs: list[str] = []
    installer = ""
    for line in stdout.strip().splitlines():
        if line.startswith("#"):
            installer = line.lstrip("#").strip()
        elif installer == "apt" and line.strip():
            apt_pkgs.extend(line.strip().split())
    return apt_pkgs


def _try_rosdep_install(state, package: str) -> bool:
    """Resolve a rosdep key to system packages and install them.

    Uses ``rosdep resolve`` + ``apt-get install`` instead of
    ``rosdep install`` which treats arguments as ROS package names and
    fails on plain keys.
    """
    if package in state.rosdep_attempted:
        return False
    state.rosdep_attempted.add(package)
    ros_distro = os.environ.get("ROS_DISTRO", "")
    if not ros_distro:
        return False
    try:
        # Step 1: rosdep resolve → find apt package name.
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
        # Step 2: apt-get install.
        install = subprocess.run(
            ["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", *apt_pkgs],
            capture_output=True,
            text=True,
        )
        return install.returncode == 0
    except Exception:
        return False


def _ensure_fetched(state, package: str) -> bool:
    """Ensure a lockfile package is fully fetched (has package.xml on disk).

    Performs inline git sparse-checkout to fetch the package directory if needed.
    Updates _state.package_shares with the on-disk path after fetching.

    Returns True if the package is available after this call.
    Returns False if the package is not in the lockfile or fetching failed.
    """
    if package in state.fetched_packages:
        return True

    pkg_info = state.lockfile_data.get(package)
    if not pkg_info or not state.fetch_dir:
        return False

    repo_workspace_path = pkg_info["repo"]
    pkg_path_in_repo = pkg_info["path"]
    repo_url = pkg_info["url"]
    repo_sha = pkg_info["version"]

    repo_dir = os.path.join(state.fetch_dir, repo_workspace_path)
    pkg_dir = os.path.join(repo_dir, pkg_path_in_repo)

    # Check if already fully fetched (package.xml present).
    if os.path.isfile(os.path.join(pkg_dir, "package.xml")):
        state.fetched_packages.add(package)
        state.package_shares[package] = pkg_dir
        return True

    # Need to fetch via git sparse-checkout.
    try:
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            # Repo not cloned yet — sparse clone.
            os.makedirs(repo_dir, exist_ok=True)
            # Normalize sparse path: "." means repo root → use "/**"
            sparse_path = "/**" if pkg_path_in_repo in (".", "") else pkg_path_in_repo
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
            # Repo exists — add to sparse-checkout.
            sparse_path = "/**" if pkg_path_in_repo in (".", "") else pkg_path_in_repo
            subprocess.run(
                ["git", "sparse-checkout", "add", sparse_path],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )

        # Verify fetch succeeded.
        if os.path.isfile(os.path.join(pkg_dir, "package.xml")):
            state.fetched_packages.add(package)
            state.package_shares[package] = pkg_dir
            return True
        else:
            logger.warning("fetched package '%s' but package.xml not found at %s", package, pkg_dir)
            return False

    except subprocess.CalledProcessError as e:
        logger.warning("git fetch failed for package '%s': %s", package, e.stderr or e)
        return False
    except Exception as e:
        logger.warning("failed to fetch package '%s': %s", package, e)
        return False


# ─── Portable path support ────────────────────────────────────────────────────
#
# A "portable path" is a string in $(find-pkg-share <pkg>)/... format.  It is
# the canonical path representation used internally by launch-plus in all modes.
# Actual filesystem resolution happens at:
#   1. Include expansion (resolver reads the included file inline).
#   2. File access inside OpaqueFunction bodies (via _stub_open / os.path stubs).
#
# This regex extracts the package name and the optional suffix from a portable path.

_PORTABLE_PATH_RE = re.compile(r"^\$\(find-pkg-share ([^)]+)\)(.*)")


def _parse_portable_path(path_str: str):
    """Extract ``(pkg_name, rest)`` from a portable path string, or return ``None``.

    ``rest`` is the portion after the closing ``)`` with any leading separator stripped.
    For example::

        "$(find-pkg-share my_pkg)/config/file.yaml"  →  ("my_pkg", "config/file.yaml")
        "$(find-pkg-share my_pkg)"                   →  ("my_pkg", "")
        "/absolute/path"                             →  None
    """
    m = _PORTABLE_PATH_RE.match(path_str)
    if not m:
        return None
    pkg = m.group(1).strip()
    rest = m.group(2).lstrip("/").lstrip(os.sep)
    return pkg, rest


def _resolve_pkg_share(state, package: str) -> str:
    """Resolve a package share path.

    Preview mode (lockfile workflow):
      Lockfile packages resolve from the workspace source tree.  If the source
      directory is not yet fully fetched (no package.xml), _ensure_fetched() is
      called to fetch it inline.

    Non-preview mode (postbuild):
      Only AMENT_PREFIX_PATH (installed artifacts) is used.  Source paths are
      never returned — if a package is not installed, the portable fallback is
      returned.

    Non-lockfile packages (system / rosdep) are resolved via AMENT_PREFIX_PATH
    in both modes.
    """
    # 1. state.package_shares lookup.
    #    Preview: contains workspace source paths.
    #    Postbuild: contains install paths from AMENT_PREFIX_PATH.
    if package in state.package_shares:
        pkg_path: str = state.package_shares[package]
        # In preview mode, lockfile packages may need full fetch.
        if (
            state.preview_mode
            and state.lockfile_data
            and package in state.lockfile_data
            and not os.path.isfile(os.path.join(pkg_path, "package.xml"))
        ):
            if _ensure_fetched(state, package):
                return str(state.package_shares[package])
            logger.error("failed to fetch package '%s' from lockfile", package)
            return f"$(find-pkg-share {package})"
        return pkg_path
    # 2. Lockfile package not yet in state.package_shares — fetch (preview only).
    if state.preview_mode and state.lockfile_data and package in state.lockfile_data:
        if _ensure_fetched(state, package):
            return str(state.package_shares[package])
        logger.error("failed to fetch package '%s' from lockfile", package)
        return f"$(find-pkg-share {package})"
    # 3. Non-lockfile packages (system / rosdep): use AMENT_PREFIX_PATH.
    if _real_get_package_share_directory is not None:
        try:
            return str(_real_get_package_share_directory(package))
        except Exception:
            pass  # Fall through to rosdep or portable fallback.
    # 4. Try rosdep install if enabled.
    if (
        state.rosdep_fallback
        and _try_rosdep_install(state, package)
        and _real_get_package_share_directory is not None
    ):
        try:
            return str(_real_get_package_share_directory(package))
        except Exception:
            pass
    # 5. Fallback.
    if state.preview_mode:
        # Preview: return portable syntax — downstream open() will hit the
        # FileNotFoundError stub which emits a warning.
        return f"$(find-pkg-share {package})"
    # Postbuild: package not installed — this is an error.
    raise LookupError(f"package '{package}' not found in AMENT_PREFIX_PATH")


def _resolve_ros_substitutions(state, value: str) -> str:
    """Replace $(find-pkg-share X) patterns in a string with real paths."""
    return re.sub(
        r"\$\(find-pkg-share ([^)]+)\)",
        lambda m: _resolve_pkg_share(state, m.group(1).strip()),
        value,
    )


# ─── Result accumulator ──────────────────────────────────────────────────────


def _is_substitution(value):
    """Return True if *value* is a launch substitution (not yet resolved to a string).

    Handles both single substitution objects (with a ``perform`` method) and
    list-of-substitutions (``SomeSubstitutionsType`` in ROS 2), where any
    element may be a substitution object.
    """
    if value is None or isinstance(value, str):
        return False
    if hasattr(value, "perform"):
        return True
    if isinstance(value, (list, tuple)):
        return any(hasattr(item, "perform") for item in value)
    return False


def _track_package(state, pkg):
    if not pkg:
        return
    # Skip substitution objects (e.g. LaunchConfiguration) — the variable name
    # is NOT a real package.  The resolved value will be tracked later in
    # _resolve_node_details / _resolve_composable_plugins.
    if _is_substitution(pkg):
        return
    pkg = str(pkg)
    if pkg and pkg not in state.tracked["packages"]:
        state.tracked["packages"].append(pkg)


def _extract_pkg_and_share_path(path_str: str):
    """Extract (package, share_path) from a portable or AMENT install path.

    Handles two forms:
      - Portable: ``$(find-pkg-share pkg)/launch/foo.py`` → ``("pkg", "launch/foo.py")``
      - AMENT:    ``/opt/.../share/pkg/launch/foo.py``    → ``("pkg", "launch/foo.py")``

    Returns ``None`` if neither form matches.
    """
    parsed = _parse_portable_path(path_str)
    if parsed:
        return parsed
    # AMENT install path: .../share/<package>/<rest>
    idx = path_str.find("/share/")
    if idx >= 0 and "$(" not in path_str:
        after_share = path_str[idx + 7 :]  # skip "/share/"
        slash = after_share.find("/")
        if slash > 0:
            pkg = after_share[:slash]
            rest = after_share[slash + 1 :]
            if rest:
                return pkg, rest
    return None


def _track_include(state, path):
    if not path:
        return
    path = str(path)
    if path not in state.tracked["includes"]:
        state.tracked["includes"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {
            "package": dep[0],
            "share_path": dep[1],
            "path": path,
            "namespace_stack": list(state.namespace_stack),
        }
        # Don't deduplicate: the same file may be included multiple times under
        # different <push-ros-namespace> contexts, and each entry carries a distinct
        # namespace_stack needed for correct namespace propagation.
        entry["include_args"] = {}
        state.tracked["include_deps"].append(entry)
    # Return the index of the last entry with this path so callers can
    # attach include_args to the correct entry.
    for i in range(len(state.tracked["include_deps"]) - 1, -1, -1):
        if state.tracked["include_deps"][i].get("path") == path:
            return i
    return -1


def _track_param_file(state, path):
    if not path:
        return
    path = str(path)
    if path not in state.tracked["param_files"]:
        state.tracked["param_files"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {"package": dep[0], "share_path": dep[1]}
        if entry not in state.tracked["param_file_deps"]:
            state.tracked["param_file_deps"].append(entry)


def _current_source_key(state) -> str:
    """Return the source key for the current file being resolved.

    Uses the last entry of state.include_chain, or state.root_source_key for root-level.
    Format: "pkg://share_path".
    """
    if state.include_chain:
        pkg, path = state.include_chain[-1]
        return f"{pkg}://{path}" if pkg else path
    return str(state.root_source_key)


def _portable_display(sub) -> str:
    """Get the portable display string for a substitution without triggering resolution.

    Unlike ``str(sub)`` which may call ``_resolve_pkg_share()`` in non-preview
    mode, this always returns the portable form (e.g. ``$(find-pkg-share pkg)``).
    Used for recording unresolved declared arg defaults in --show-args metadata.
    """
    if isinstance(sub, _TrackedFindPackageShare):
        pkg, _ = sub._resolve_name(None)
        return f"$(find-pkg-share {pkg})"
    if isinstance(sub, _TrackedPathJoinSubstitution):
        return "/".join(_portable_display(s) for s in sub._subs)
    return str(sub)


def _record_declared_arg(state, name: str, default: str, *, flat: bool = True) -> None:
    """Record a declared arg in the per-file dict, and optionally the flat list.

    The flat list is used for apply_arg_defaults (first-declaration wins).
    The per-file dict is used by --show-args to render arg comments per included file.
    """
    if flat:
        state.tracked["declared_args"].append({"name": name, "default": default})
    key = _current_source_key(state)
    if key:
        by_file = state.tracked["declared_args_by_file"]
        if key not in by_file:
            by_file[key] = []
        by_file[key].append({"name": name, "default": default})


def _track_node(state, node_dict: dict) -> int:
    """Append a node dict to state.tracked["nodes"] with source info from state.include_chain.

    Returns the index of the appended node.
    """
    if state.include_chain:
        node_dict["include_chain"] = list(state.include_chain)
    idx = len(state.tracked["nodes"])
    state.tracked["nodes"].append(node_dict)
    return idx


def _track_event_handler(state, eh_dict: dict) -> int:
    """Track an event handler as a node entry so it appears in encounter order.

    Wraps the event handler dict in a node-shaped dict with ``kind: "event_handler"``
    and uses ``_track_node()`` to get proper ``include_chain`` and ordering.
    """
    node_dict = {
        "package": "",
        "executable": "",
        "name": "",
        "namespace_stack": eh_dict.get("namespace_stack", []),
        "explicit_namespace": eh_dict.get("explicit_namespace"),
        "parameters": {},
        "param_files": [],
        "remappings": [],
        "env": {},
        "kind": "event_handler",
        "plugins": [],
        "target": eh_dict.get("target"),
        # Event-handler-specific fields
        "handler_kind": eh_dict.get("handler_kind", ""),
        "target_node": eh_dict.get("target_node"),
        "start_state": eh_dict.get("start_state"),
        "goal_state": eh_dict.get("goal_state"),
        "eh_actions": eh_dict.get("actions", []),
    }
    return _track_node(state, node_dict)


def _to_str(value: object, context: Any = None) -> str | None:
    """Coerce a str, substitution object, or None to ``str | None``.

    - ``None`` → ``None`` (caller decides how to handle missing values)
    - ``str``  → returned as-is
    - object with ``.perform()`` → call it; ``None`` result stays ``None``,
      non-``None`` result is coerced to ``str``; on exception → ``str(value)``
    - anything else → ``str(value)``

    Every non-``None`` return value is guaranteed to be ``str``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "perform"):
        try:
            res = value.perform(context)
            return str(res) if res is not None else None
        except Exception:
            return str(value)
    return str(value)


def _resolve_substitution(sub: object, context: Any) -> str | None:
    """Resolve a substitution, list-of-substitutions, or plain string to str."""
    value, _fallback = _resolve_substitution_ex(sub, context)
    return value


def _resolve_substitution_ex(sub: object, context: Any) -> tuple[str | None, bool]:
    """Resolve a substitution and report whether the result is a fallback.

    Returns ``(resolved_str_or_None, is_fallback)`` where *is_fallback* is
    ``True`` when ``perform()`` returned ``None`` or raised and the display
    name was used instead.  Callers that need to distinguish "really resolved"
    from "fell back to variable name" (e.g. package tracking) should use this.
    """
    if sub is None:
        return None, False
    if isinstance(sub, str):
        # Plain strings from Python launch code are already resolved (they come from
        # Python expressions, not unresolved XML/YAML text).  Do NOT call
        # _resolve_ros_substitutions here — that would eagerly expand portable
        # $(find-pkg-share ...) paths back to machine-specific filesystem paths.
        return sub, False
    if isinstance(sub, (list, tuple)):
        parts = []
        any_fallback = False
        for s in sub:
            if hasattr(s, "perform"):
                if context is not None:
                    try:
                        result = s.perform(context)
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
                    any_fallback = True
            else:
                parts.append(str(s))
        return "".join(parts), any_fallback
    if context is not None and hasattr(sub, "perform"):
        try:
            result = sub.perform(context)
            if result is None:
                return str(sub), True  # unresolved — use display name
            return str(result), False
        except Exception:
            return str(sub), True
    return str(sub), True


def _track_node_from_action(state, package, executable, name=None):
    """Track a node from an unpatched ROS 2 action (e.g. from OpaqueFunction return)."""
    # Pass raw package to _track_package BEFORE stringifying — _track_package
    # has an _is_substitution guard that filters out substitution objects.
    _track_package(state, package)
    package = str(package) if package else ""
    executable = str(executable) if executable else ""
    name = str(name) if name else ""
    return _track_node(
        state,
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
        },
    )


# ─── XML/YAML Launch File Parser ──────────────────────────────────────────────
#
# Parsing is delegated to Entity-based parsers in ``launch_plus.parsers``.
# Resolution is dispatched through the action registry via _resolve_element().

import xml.etree.ElementTree as ET  # noqa: F401 — still used by callers

from launch_plus.parsers.entity import Entity
from launch_plus.parsers.xml_parser import parse_xml_launch as _parse_xml_launch_entity
from launch_plus.parsers.yaml_parser import parse_yaml_launch as _parse_yaml_launch_entity


def parse_xml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse an XML launch file to a list of Entity objects."""
    return list(_parse_xml_launch_entity(content, file_path))


def parse_yaml_launch(content: str, file_path: str) -> list[Entity]:
    """Parse a YAML launch file to a list of Entity objects."""
    return list(_parse_yaml_launch_entity(content, file_path))


# ─── XML/YAML resolution engine (extracted to entities/xml_resolver.py) ──────
# ── Action handler registration ───────────────────────────────────────────────
#
# Action handlers live in launch_plus.entities.actions.*.  Importing the
# package triggers @expose_action registration into action_parse_methods.
# ─── Param-file stubs for OpaqueFunction resilience ─────────────────────────
#
# OpaqueFunction bodies in Autoware typically call `open(param_file)` and
# `yaml.safe_load(f)` to read ROS 2 parameter YAML files before constructing
# their composable nodes.  In preview mode these paths are resolved to
# workspace-relative strings which may not exist (e.g. naming mismatches
# between the autoware_launch config and the package's own config directory).
#
# To recover from this edge case we temporarily patch `builtins.open` and
# `yaml.safe_load` during OpaqueFunction execution:
#   - open(): if the requested file is not found, emit a warning and return a
#     StringIO stub containing a minimal valid ROS 2 parameter YAML.
#   - yaml.safe_load(): wraps any `ros__parameters` dict in `_DefaultParamDict`
#     so that missing keys return `False` instead of raising `KeyError`.
#     Boolean-flag guards such as `if params["downsample_input_pointcloud"]:`
#     then evaluate to False, skipping optional branches while still letting
#     the function reach and register its core composable nodes.
#
# The patches are installed/removed atomically around `fn(context)` via
# `_call_opaque_with_stubs`.

import launch_plus.entities.actions  # noqa: F401, E402
from launch_plus.entities.actions.arg import _apply_declared_arg, _DeclaredArg  # noqa: E402

# ─── Shim classes (re-exported for backward compatibility) ──────���────────────
from launch_plus.entities.actions.base import _TrackedAction  # noqa: E402
from launch_plus.entities.actions.env import (  # noqa: E402, F401
    _TrackedSetEnvironmentVariable,
    _TrackedUnsetEnvironmentVariable,
)
from launch_plus.entities.actions.group import _TrackedGroupAction  # noqa: E402, F401
from launch_plus.entities.actions.node import (  # noqa: E402, F401
    _TrackedComposableNode,
    _TrackedComposableNodeContainer,
    _TrackedNode,
)

# ─── OpaqueFunction stubs (extracted to entities/opaque_stubs.py) ─────────────
from launch_plus.entities.opaque_stubs import (  # noqa: E402, F401
    _STUB_ROS_PARAM_YAML,
    _call_opaque_with_stubs,
    _DefaultParamDict,
)
from launch_plus.entities.substitutions.find_pkg_share import (
    _TrackedFindPackageShare,  # noqa: E402, F401
)
from launch_plus.entities.substitutions.launch_config import (  # noqa: E402, F401
    _DeferredDefault,
    _LaunchConfiguration,
)
from launch_plus.entities.substitutions.path_join import _TrackedPathJoinSubstitution  # noqa: E402
from launch_plus.entities.xml_resolver import (  # noqa: E402, F401
    _ActionParser,
    _effective_namespace,
    _evaluate_condition,
    _is_truthy,
    _read_and_expand_param_file,
    _resolve_element,
    _SubstitutionContext,
    resolve_substitutions,
    resolve_substitutions_from_tokens,
    resolve_xml_elements,
)

# ─── LaunchContext factory ────────────────────────────────────────────────────


def _make_launch_context(args_dict):
    """Create a LaunchContext pre-populated with provided args."""
    try:
        from launch import LaunchContext

        ctx = LaunchContext()
        ctx._launch_configurations = dict(args_dict)
        return ctx
    except Exception:
        ctx = _StubLaunchContext()
        ctx._launch_configurations = dict(args_dict)
        return ctx


# ─── Node detail resolution (extracted to entities/node_resolution.py) ────────
from launch_plus.entities.node_resolution import (  # noqa: E402, F401
    _env_overrides,
    _resolve_composable_plugins,
    _resolve_node_details,
)

# ─── Inline Python include resolution ─────────────────────────────────────────


def _inline_resolve_python_launch(state, launch_file, parent_context, child_args):
    """Load a Python launch file and walk its actions in the parent context.

    This mirrors real ROS 2 behavior where ``IncludeLaunchDescription``
    synchronously executes the child, so ``SetLaunchConfiguration`` calls in
    the child mutate the shared ``LaunchContext``.
    """

    # Resolve portable paths — $(find-pkg-share pkg)/rest → real filesystem path.
    real_path = launch_file
    parsed = _parse_portable_path(launch_file)
    if parsed:
        pkg, rest = parsed
        try:
            pkg_share = _resolve_pkg_share(state, pkg)
        except Exception:
            return  # Package not available
        real_path = os.path.join(pkg_share, rest)

    if not os.path.isfile(real_path):
        return  # File not on disk

    try:
        spec = importlib.util.spec_from_file_location(
            f"_inline_launch_{state.walk_depth}", real_path
        )
        if spec is None or spec.loader is None:
            logger.warning("cannot load included launch file: %s", real_path)
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.warning("failed to load included launch file %s: %s", real_path, e)
        return

    if not hasattr(mod, "generate_launch_description"):
        return

    # Save scoping state that must be restored after inline execution.
    # Nodes, includes, packages, params, etc. are KEPT — the resolver
    # handles all includes inline.
    saved_declared_arg_names = set(state.declared_arg_names)
    saved_namespace_depth = len(state.namespace_stack)
    saved_env = dict(state.env)

    # Snapshot parent context BEFORE generate_launch_description() so we can
    # fully restore it after the inline walk.  Only deliberate side-effects
    # (SetLaunchConfiguration) survive.
    saved_configs = dict(parent_context._launch_configurations)

    # Most _state.tracked mutations (from constructors in generate_launch_description
    # AND from _walk_actions) must be rolled back on any exit path, including
    # exceptions from generate_launch_description() itself.
    #
    # Intentionally preserved side-effects (NOT rolled back):
    #   - set_launch_configurations: the whole purpose of inline includes
    #   - warnings / errors: diagnostic messages should propagate to the user
    # Everything else in _state.tracked is rolled back in the finally block below.
    try:
        try:
            ld = mod.generate_launch_description()
        except Exception as e:
            logger.warning("generate_launch_description() failed in %s: %s", launch_file, e)
            return

        entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

        # Apply child launch arguments: explicit include args override parent context.
        # In strict mode (global_arg_cascade=False), strip parent context so the
        # child only sees explicitly forwarded args + its own DeclareLaunchArgument.
        if not state.global_arg_cascade:
            parent_context._launch_configurations.clear()
        for k, v in child_args.items():
            parent_context._launch_configurations[k] = v

        # Pass 1: apply DeclareLaunchArgument defaults (child-only args).
        for entity in entities:
            if isinstance(entity, _DeclaredArg):
                _apply_declared_arg(entity, parent_context)

        # Pass 2: walk actions — SetLaunchConfiguration, OpaqueFunction, etc.
        # all mutate parent_context directly, which is the desired effect.
        # Only context mutations survive; tracked state is rolled back.
        _walk_actions(state, entities, parent_context)
    finally:
        # Restore scoping state only — nodes, includes, packages, params, etc.
        # are intentionally kept (includes are resolved inline).
        state.declared_arg_names.clear()
        state.declared_arg_names.update(saved_declared_arg_names)
        del state.namespace_stack[saved_namespace_depth:]
        state.env.clear()
        state.env.update(saved_env)

    # Restore child-only args that were not SetLaunchConfiguration'd —
    # child DeclareLaunchArgument defaults should NOT leak into the parent
    # scope, only SetLaunchConfiguration is a deliberate side-effect.
    # Keep keys that were either already in the parent or were set via
    # SetLaunchConfiguration.  Also preserve "global_params" — this is the
    # accumulation list for SetParameter, not a launch argument.
    set_configs = set(state.tracked["set_launch_configurations"].keys())
    for k in list(parent_context._launch_configurations):
        if k not in saved_configs and k not in set_configs and k != "global_params":
            del parent_context._launch_configurations[k]


# ─── Action walker ────────────────────────────────────────────────────────────


def _walk_actions(state, actions, context):
    """Walk a list of actions, calling ``execute()`` on each.

    Tracked actions (subclasses of ``_TrackedAction``) implement the polymorphic
    ``execute(context)`` method.  Untracked actions (real ROS 2 objects from
    OpaqueFunction returns) are handled by ``_walk_untracked_action()``.
    """
    if actions is None:
        return
    state.walk_depth += 1
    if state.walk_depth > 20:
        state.walk_depth -= 1
        logger.warning("Max include depth reached while walking Python launch description")
        return
    try:
        for action in actions:
            if action is None:
                continue
            try:
                if isinstance(action, _TrackedAction):
                    children = action.execute(context)
                    if children:
                        _walk_actions(state, children, context)
                else:
                    _walk_untracked_action(state, action, context)
            except Exception as e:
                logger.error("Error walking action %s: %s", type(action).__name__, e)
    finally:
        state.walk_depth -= 1


def _walk_untracked_action(state, action, context):
    """Handle real (unpatched) ROS 2 action objects.

    These come from OpaqueFunction returns that produce real ``launch_ros``
    classes instead of our shimmed ``_Tracked*`` wrappers.
    """
    cls_name = type(action).__name__

    # OpaqueFunction: execute its function and walk the result
    if cls_name == "OpaqueFunction" or (
        hasattr(action, "function") and callable(getattr(action, "function", None))
    ):
        fn = getattr(action, "function", None)
        if fn and context:
            try:
                result = _call_opaque_with_stubs(state, fn, context)
                if result:
                    _walk_actions(state, result, context)
            except Exception as e:
                logger.error("OpaqueFunction failed: %s", e)
        return

    # Walk nested actions/entities from generic action objects
    if hasattr(action, "entities"):
        try:
            _walk_actions(state, action.entities, context)
        except Exception as e:
            logger.warning("failed to walk %s.entities: %s", cls_name, e)
    if hasattr(action, "_actions"):
        try:
            _walk_actions(state, action._actions, context)
        except Exception as e:
            logger.warning("failed to walk %s._actions: %s", cls_name, e)

    # Real Node/LifecycleNode from launch_ros (unpatched, e.g. from OpaqueFunction return)
    if cls_name in ("Node", "LifecycleNode") and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        _track_node_from_action(state, pkg, exe)
    elif cls_name == "ComposableNodeContainer" and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        name = getattr(action, "_name", None)
        _track_node_from_action(state, pkg, exe, name)
        descs = (
            getattr(
                action,
                "composable_node_descriptions",
                getattr(action, "_composable_node_descriptions", None),
            )
            or []
        )
        for desc in descs:
            _track_node_from_action(
                state,
                getattr(desc, "package", None),
                getattr(desc, "plugin", None),
                getattr(desc, "node_name", getattr(desc, "name", None)),
            )
    elif cls_name == "LoadComposableNodes":
        descs = (
            getattr(
                action,
                "_composable_node_descriptions",
                getattr(action, "composable_node_descriptions", None),
            )
            or []
        )
        for desc in descs:
            _track_node_from_action(
                state,
                getattr(desc, "package", None),
                getattr(desc, "plugin", None),
                getattr(desc, "node_name", getattr(desc, "name", None)),
            )

    # IncludeLaunchDescription (real)
    if cls_name == "IncludeLaunchDescription":
        src = getattr(action, "_launch_description_source", None)
        if src:
            loc = getattr(src, "location", None)
            if callable(loc) and context:
                try:
                    loc = loc(context)
                except Exception:
                    loc = None
            if loc:
                _track_include(state, loc)

    # Warn about action classes we do not recognise.
    if cls_name not in _KNOWN_UNTRACKED_CLASSES:
        logger.warning(
            "Unrecognised action type '%s' — any nodes or includes it "
            "declares may not appear in the resolved output",
            cls_name,
        )


# ─── Known untracked action class names ──────────────────────────────────────

# Real ROS 2 class names that _walk_untracked_action handles or that are safe
# to skip.  Our _TrackedAction subclasses are dispatched via execute() and
# don't need to be listed here.
_KNOWN_UNTRACKED_CLASSES: frozenset = frozenset(
    {
        # Real launch_ros classes handled via cls_name duck-typing
        "Node",
        "LifecycleNode",
        "ComposableNodeContainer",
        "LoadComposableNodes",
        "IncludeLaunchDescription",
        # ROS 2 infrastructure — safe to skip (no nodes/includes produced)
        "LogInfo",
        "RegisterEventHandler",
        "EmitEvent",
        "Shutdown",
        "ExecuteProcess",
        "DeclareLaunchArgument",
        "SetLaunchConfiguration",
        "SetParameter",
        "PushRosNamespace",
        "ResetLaunchConfigurations",
        "AppendEnvironmentVariable",
        "SetEnvironmentVariable",
        "UnsetEnvironmentVariable",
        "OpaqueCoroutine",
        "OpaqueFunction",
        "GroupAction",
        "TimerAction",
    }
)

# ─── Import system patcher (extracted to entities/import_patcher.py) ──────────
from launch_plus.entities.import_patcher import (  # noqa: E402, F401
    _PATCHED_MODULES,
    _PatchingFinder,
)


def resolve_file(
    launch_file: "Path",
    args: dict[str, str],
    package_shares: dict[str, str],
    workflow_options: Any = None,
    lockfile: Any = None,
    fetch_dir: "Path | None" = None,
    global_params: list | None = None,
) -> Any:
    """Resolve a launch file and return a ParsedLaunchFile.

    Parameters
    ----------
    launch_file : Path
        Absolute path to the launch file.
    args : dict
        Launch arguments (``name:=value`` pairs).
    package_shares : dict
        Package name → share directory path.
    workflow_options : ResolveWorkflowOptions
        Workflow flags.
    lockfile : Lockfile
        Lockfile with package/repo info.
    fetch_dir : Path | None
        Directory for sparse-checkout.
    global_params : list | None
        Persisted global params from prior files.

    Returns
    -------
    ParsedLaunchFile
        The resolver result as a structured object (imported from types module).
    """
    # ── Create scoped state + diagnostic collector ─────────────────────
    from launch_plus.log import DiagnosticCollector

    state = ResolverState()
    collector = DiagnosticCollector()
    logging.getLogger("launch_plus").addHandler(collector)
    token = _current_state.set(state)
    try:
        result = _resolve_file_impl(
            state,
            launch_file,
            args,
            package_shares,
            workflow_options,
            lockfile,
            fetch_dir,
            global_params,
        )
        # Populate ParsedLaunchFile diagnostics from the collector
        result.warnings.extend(collector.warnings)
        result.errors.extend(collector.errors)
        return result
    finally:
        _current_state.reset(token)
        logging.getLogger("launch_plus").removeHandler(collector)


def _resolve_file_impl(
    state: ResolverState,
    launch_file: "Path",
    args: dict[str, str],
    package_shares: dict[str, str],
    workflow_options: Any = None,
    lockfile: Any = None,
    fetch_dir: "Path | None" = None,
    global_params: list | None = None,
) -> Any:
    launch_file_str = str(launch_file)

    state.namespace_stack = []
    root_dep = _extract_pkg_and_share_path(launch_file_str)
    state.root_source_key = f"{root_dep[0]}://{root_dep[1]}" if root_dep else launch_file_str

    state.env.clear()
    state.global_params.clear()
    state.global_remaps.clear()
    state.global_param_files.clear()
    state.fetched_packages.clear()
    state.declared_arg_names.clear()
    state.include_chain.clear()
    state.package_shares = dict(package_shares)

    # Reset state.tracked
    state.tracked["packages"] = []
    state.tracked["includes"] = []
    state.tracked["nodes"] = []
    state.tracked["declared_args"] = []
    state.tracked["declared_args_by_file"] = {}
    state.tracked["global_params"] = []
    state.tracked["include_args"] = {}
    state.tracked["param_files"] = []
    state.tracked["set_launch_configurations"] = {}
    state.tracked["include_deps"] = []
    state.tracked["param_file_deps"] = []
    state.tracked["event_handlers"] = []

    # Workflow flags
    if workflow_options is not None:
        state.apply_opaque_file_access = bool(
            getattr(workflow_options, "apply_opaque_file_access", False)
        )
        state.preview_mode = bool(getattr(workflow_options, "preview", True))
        state.inline_params = bool(getattr(workflow_options, "inline_params", False))
        state.rosdep_fallback = bool(getattr(workflow_options, "rosdep_fallback", False))
        state.apply_arg_defaults = bool(getattr(workflow_options, "apply_arg_defaults", False))
        state.global_arg_cascade = bool(getattr(workflow_options, "global_arg_cascade", False))
        state.allow_unportable_paths = bool(
            getattr(workflow_options, "allow_unportable_paths", False)
        )
    else:
        state.apply_opaque_file_access = False
        state.preview_mode = True
        state.inline_params = False
        state.rosdep_fallback = False
        state.apply_arg_defaults = False
        state.global_arg_cascade = False
        state.allow_unportable_paths = False

    # Build lockfile data from the Lockfile dataclass
    if lockfile is not None:
        lf_data = {}
        for pkg_name, pkg_lock in lockfile.packages.items():
            repo_lock = lockfile.repositories.get(pkg_lock.repo)
            if repo_lock is not None:
                lf_data[pkg_name] = {
                    "repo": pkg_lock.repo,
                    "path": pkg_lock.path,
                    "url": repo_lock.url,
                    "version": repo_lock.version,
                }
        state.lockfile_data = lf_data
    else:
        state.lockfile_data = {}

    state.fetch_dir = str(fetch_dir) if fetch_dir else ""

    args_dict = dict(args)

    # Pre-populate global params
    persisted_global_params = list(global_params) if global_params else []

    # Install the import patcher
    sys.meta_path.insert(0, _PatchingFinder())
    for mod_name, builder in _PatchingFinder.PATCHED.items():
        if mod_name not in _PATCHED_MODULES:
            _PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = _PATCHED_MODULES[mod_name]

    # ── Resolve by file type ─────────────────────────────────────────────
    if launch_file_str.endswith((".launch.xml", ".xml", ".yaml", ".yml")):
        try:
            with open(launch_file_str) as f:
                content = f.read()
        except Exception as e:
            logger.error("cannot read %s: %s", launch_file_str, e)
            return _tracked_to_parsed_launch_file(state.tracked)

        if launch_file_str.endswith((".yaml", ".yml")):
            elements = parse_yaml_launch(content, launch_file_str)
        else:
            elements = parse_xml_launch(content, launch_file_str)
        subst_ctx = _SubstitutionContext(state)
        subst_ctx.args = dict(args_dict)
        subst_ctx.launch_file_dir = os.path.dirname(os.path.abspath(launch_file_str))
        subst_ctx.preview_mode = state.preview_mode
        subst_ctx.env = state.env
        resolve_xml_elements(elements, subst_ctx, include_stack=[launch_file_str])
        return _tracked_to_parsed_launch_file(state.tracked)

    # ── Python launch files ──────────────────────────────────────────────
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file_str)
    if spec is None or spec.loader is None:
        logger.error("cannot load %s", launch_file_str)
        return _tracked_to_parsed_launch_file(state.tracked)

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.error("Error loading launch file: %s", e)
        return _tracked_to_parsed_launch_file(state.tracked)

    if not hasattr(mod, "generate_launch_description"):
        logger.error("No generate_launch_description() function found")
        return _tracked_to_parsed_launch_file(state.tracked)

    # Inject persisted global params
    if "__global_params__" in args_dict:
        try:
            persisted_global_params = json.loads(args_dict.pop("__global_params__"))
        except Exception:
            pass

    ctx = _make_launch_context(args_dict)

    if persisted_global_params:
        gp_tuples = [(entry[0], entry[1]) for entry in persisted_global_params if len(entry) == 2]
        ctx._launch_configurations["global_params"] = list(gp_tuples)
        state.global_params.extend(gp_tuples)

    try:
        ld = mod.generate_launch_description()
    except Exception as e:
        logger.error("generate_launch_description() failed: %s", e)
        return _tracked_to_parsed_launch_file(state.tracked)

    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, _DeclaredArg):
            _apply_declared_arg(entity, ctx)

    _walk_actions(state, entities, ctx)

    return _tracked_to_parsed_launch_file(state.tracked)


def _tracked_to_parsed_launch_file(tracked: dict[str, Any]) -> Any:
    """Convert the internal _state.tracked dict to a ParsedLaunchFile."""
    from pathlib import Path as _Path

    from launch_plus.types import (
        ComposablePlugin as _ComposablePlugin,
    )
    from launch_plus.types import (
        DependencyKind as _DependencyKind,
    )
    from launch_plus.types import (
        EventHandlerKind as _EventHandlerKind,
    )
    from launch_plus.types import (
        FileDependency as _FileDependency,
    )
    from launch_plus.types import (
        LaunchInclude as _LaunchInclude,
    )
    from launch_plus.types import (
        NodeKindTag as _NodeKindTag,
    )
    from launch_plus.types import (
        ParamFileInlined as _ParamFileInlined,
    )
    from launch_plus.types import (
        ParamFileReference as _ParamFileReference,
    )
    from launch_plus.types import (
        ParsedLaunchFile as _ParsedLaunchFile,
    )
    from launch_plus.types import (
        ResolvedEventAction as _ResolvedEventAction,
    )
    from launch_plus.types import (
        ResolvedNode as _ResolvedNode,
    )
    # _effective_namespace is imported at module level from xml_resolver

    # ── Convert nodes ────────────────────────────────────────────────────
    resolved_nodes: list[_ResolvedNode] = []
    for n in tracked.get("nodes", []):
        kind_str = n.get("kind", "node")
        if kind_str == "set_parameter":
            continue

        ns_stack = n.get("namespace_stack", [])
        explicit_ns = n.get("explicit_namespace")
        eff_ns = _effective_namespace(ns_stack, explicit_ns)
        name = n.get("name", "")

        # Map kind
        kind_tag: _NodeKindTag
        plugins: list[_ComposablePlugin] = []
        load_target: str | None = None
        log_message: str | None = None
        remap_from: str | None = None
        remap_to: str | None = None
        exec_cmd: str | None = None
        exec_name: str | None = None
        exec_shell: bool = False
        handler_kind: _EventHandlerKind | None = None
        handler_target: str | None = None
        handler_target_node: str | None = None
        handler_namespace: str | None = None
        handler_start_state: str | None = None
        handler_goal_state: str | None = None
        handler_actions: list[_ResolvedEventAction] = []

        if kind_str == "container":
            kind_tag = _NodeKindTag.CONTAINER
            plugins = _convert_plugins(n.get("plugins", []))
        elif kind_str == "load_composable":
            kind_tag = _NodeKindTag.LOAD_COMPOSABLE
            load_target = n.get("target", "")
            plugins = _convert_plugins(n.get("plugins", []))
        elif kind_str == "lifecycle_node":
            kind_tag = _NodeKindTag.LIFECYCLE_NODE
        elif kind_str == "set_remap":
            kind_tag = _NodeKindTag.SET_REMAP
            remap_from = n.get("remap_from", "")
            remap_to = n.get("remap_to", "")
        elif kind_str == "log":
            kind_tag = _NodeKindTag.LOG
            log_message = n.get("message", "")
        elif kind_str == "executable":
            kind_tag = _NodeKindTag.EXECUTABLE
            exec_cmd = n.get("cmd", "")
            exec_name = name if name else None
            exec_shell = n.get("shell", False)
        elif kind_str == "event_handler":
            kind_tag = _NodeKindTag.EVENT_HANDLER
            hk_str = n.get("handler_kind", "")
            hk_map = {
                "on_process_start": _EventHandlerKind.ON_PROCESS_START,
                "on_process_exit": _EventHandlerKind.ON_PROCESS_EXIT,
                "on_state_transition": _EventHandlerKind.ON_STATE_TRANSITION,
                "on_shutdown": _EventHandlerKind.ON_SHUTDOWN,
            }
            handler_kind = hk_map.get(hk_str)
            if handler_kind is None:
                continue
            handler_target = n.get("target")
            handler_target_node = n.get("target_node")
            handler_namespace = _effective_namespace(ns_stack, explicit_ns)
            handler_start_state = n.get("start_state")
            handler_goal_state = n.get("goal_state")
            handler_actions = [
                _ResolvedEventAction(
                    event=a.get("event", ""),
                    target_node=a.get("target_node"),
                    namespace=a.get("explicit_namespace"),
                )
                for a in n.get("eh_actions", [])
            ]
        else:
            kind_tag = _NodeKindTag.NODE

        # Include chain
        chain_raw = n.get("include_chain", [])
        include_chain = [(pkg, _Path(sp)) for pkg, sp in chain_raw]
        source = include_chain[-1] if include_chain else None

        # Param files
        param_files: list[_ParamFileReference | _ParamFileInlined] = []
        for pf in n.get("param_files", []):
            if pf.get("params") is not None:
                param_files.append(_ParamFileInlined(display=pf["path"], params=pf["params"]))
            else:
                param_files.append(_ParamFileReference(display=pf["path"], abs=pf["path"]))

        resolved_nodes.append(
            _ResolvedNode(
                package=n.get("package", ""),
                executable=n.get("executable", ""),
                name=name if name else None,
                namespace=eff_ns,
                explicit_namespace=explicit_ns,
                namespace_stack=list(ns_stack),
                parameters=dict(n.get("parameters", {})),
                remappings=[(f, t) for f, t in n.get("remappings", [])],
                env=dict(n.get("env", {})),
                source=source,
                include_chain=include_chain,
                kind=kind_tag,
                plugins=plugins,
                load_target=load_target,
                log_message=log_message,
                remap_from=remap_from,
                remap_to=remap_to,
                exec_cmd=exec_cmd,
                exec_name=exec_name,
                exec_shell=exec_shell,
                handler_kind=handler_kind,
                handler_target=handler_target,
                handler_target_node=handler_target_node,
                handler_namespace=handler_namespace,
                handler_start_state=handler_start_state,
                handler_goal_state=handler_goal_state,
                handler_actions=handler_actions,
                param_files=param_files,
                output=n.get("output"),
                args=n.get("args"),
                respawn=n.get("respawn"),
                respawn_delay=n.get("respawn_delay"),
            )
        )

    # ── Legacy event handlers ────────────────────────────────────────────
    has_inline_eh = any(n.kind == _NodeKindTag.EVENT_HANDLER for n in resolved_nodes)
    if not has_inline_eh:
        hk_map = {
            "on_process_start": _EventHandlerKind.ON_PROCESS_START,
            "on_process_exit": _EventHandlerKind.ON_PROCESS_EXIT,
            "on_state_transition": _EventHandlerKind.ON_STATE_TRANSITION,
            "on_shutdown": _EventHandlerKind.ON_SHUTDOWN,
        }
        for eh in tracked.get("event_handlers", []):
            hk = hk_map.get(eh.get("handler_kind", ""))
            if hk is None:
                continue
            ns_stack = eh.get("namespace_stack", [])
            explicit_ns = eh.get("explicit_namespace")
            handler_ns = _effective_namespace(ns_stack, explicit_ns)
            actions = [
                _ResolvedEventAction(
                    event=a.get("event", ""),
                    target_node=a.get("target_node"),
                    namespace=a.get("explicit_namespace"),
                )
                for a in eh.get("actions", [])
            ]
            resolved_nodes.append(
                _ResolvedNode(
                    kind=_NodeKindTag.EVENT_HANDLER,
                    namespace_stack=list(ns_stack),
                    explicit_namespace=explicit_ns,
                    handler_kind=hk,
                    handler_target=eh.get("target"),
                    handler_target_node=eh.get("target_node"),
                    handler_namespace=handler_ns,
                    handler_start_state=eh.get("start_state"),
                    handler_goal_state=eh.get("goal_state"),
                    handler_actions=actions,
                )
            )

    # ── Convert include deps ─────────────────────────────────────────────
    launch_includes: list[_LaunchInclude] = []
    include_args_map = tracked.get("include_args", {})
    for dep in tracked.get("include_deps", []):
        dep_include_args = dep.get("include_args", {})
        if not dep_include_args:
            dep_include_args = include_args_map.get(dep.get("path", ""), {})
        launch_includes.append(
            _LaunchInclude(
                package=dep["package"],
                share_path=_Path(dep["share_path"]),
                explicit_args=dep_include_args,
                namespace_stack=dep.get("namespace_stack", []),
            )
        )

    # ── Declared args ────────────────────────────────────────────────────
    declared_arg_defaults = {a["name"]: a["default"] for a in tracked.get("declared_args", [])}

    # ── Param file deps ──────────────────────────────────────────────────
    param_file_deps = [
        _FileDependency(
            package=dep["package"],
            share_path=_Path(dep["share_path"]),
            kind=_DependencyKind.PARAM,
        )
        for dep in tracked.get("param_file_deps", [])
    ]

    # ── Per-file declared args ───────────────────────────────────────────
    declared_args_by_file: dict[tuple[str, _Path], dict[str, str]] = {}
    for key_str, args_list in tracked.get("declared_args_by_file", {}).items():
        if "://" in key_str:
            idx = key_str.index("://")
            pkg = key_str[:idx]
            sp = _Path(key_str[idx + 3 :])
        else:
            pkg = ""
            sp = _Path(key_str)
        declared_args_by_file[(pkg, sp)] = {a["name"]: a["default"] for a in args_list}

    return _ParsedLaunchFile(
        packages=list(tracked.get("packages", [])),
        nodes=resolved_nodes,
        launch_includes=launch_includes,
        param_files=param_file_deps,
        declared_arg_defaults=declared_arg_defaults,
        declared_args_by_file=declared_args_by_file,
        global_params=list(tracked.get("global_params", [])),
        warnings=[],  # populated by DiagnosticCollector in resolve_file()
        errors=[],  # populated by DiagnosticCollector in resolve_file()
        infos=list(tracked.get("infos", [])) if "infos" in tracked else [],
    )


def _convert_plugins(raw_plugins: list[dict]) -> list:
    """Convert raw plugin dicts to ComposablePlugin dataclasses."""
    from launch_plus.types import (
        ComposablePlugin as _ComposablePlugin,
    )
    from launch_plus.types import (
        ParamFileInlined as _ParamFileInlined,
    )
    from launch_plus.types import (
        ParamFileReference as _ParamFileReference,
    )

    result = []
    for p in raw_plugins:
        pf_list: list[_ParamFileReference | _ParamFileInlined] = []
        for pf in p.get("param_files", []):
            if pf.get("params") is not None:
                pf_list.append(_ParamFileInlined(display=pf["path"], params=pf["params"]))
            else:
                pf_list.append(_ParamFileReference(display=pf["path"], abs=pf["path"]))
        result.append(
            _ComposablePlugin(
                package=p.get("package", ""),
                plugin=p.get("plugin", ""),
                name=p.get("name"),
                parameters=dict(p.get("parameters", {})),
                remappings=[(f, t) for f, t in p.get("remappings", [])],
                param_files=pf_list,
            )
        )
    return result
