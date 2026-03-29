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

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

# Ensure substitution entity classes are registered before any parsing occurs.
import launch_plus.entities  # noqa: F401
from launch_plus.entities.state import (  # noqa: E402
    _error,
    _state,
    _StubLaunchContext,
    _warn,
)

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


def _try_rosdep_install(package: str) -> bool:
    """Resolve a rosdep key to system packages and install them.

    Uses ``rosdep resolve`` + ``apt-get install`` instead of
    ``rosdep install`` which treats arguments as ROS package names and
    fails on plain keys.
    """
    if package in _state.rosdep_attempted:
        return False
    _state.rosdep_attempted.add(package)
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


def _ensure_fetched(package: str) -> bool:
    """Ensure a lockfile package is fully fetched (has package.xml on disk).

    Performs inline git sparse-checkout to fetch the package directory if needed.
    Updates _state.package_shares with the on-disk path after fetching.

    Returns True if the package is available after this call.
    Returns False if the package is not in the lockfile or fetching failed.
    """
    if package in _state.fetched_packages:
        return True

    pkg_info = _state.lockfile_data.get(package)
    if not pkg_info or not _state.fetch_dir:
        return False

    repo_workspace_path = pkg_info["repo"]
    pkg_path_in_repo = pkg_info["path"]
    repo_url = pkg_info["url"]
    repo_sha = pkg_info["version"]

    repo_dir = os.path.join(_state.fetch_dir, repo_workspace_path)
    pkg_dir = os.path.join(repo_dir, pkg_path_in_repo)

    # Check if already fully fetched (package.xml present).
    if os.path.isfile(os.path.join(pkg_dir, "package.xml")):
        _state.fetched_packages.add(package)
        _state.package_shares[package] = pkg_dir
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
            _state.fetched_packages.add(package)
            _state.package_shares[package] = pkg_dir
            return True
        else:
            _warn(f"fetched package '{package}' but package.xml not found at {pkg_dir}")
            return False

    except subprocess.CalledProcessError as e:
        _warn(f"git fetch failed for package '{package}': {e.stderr or e}")
        return False
    except Exception as e:
        _warn(f"failed to fetch package '{package}': {e}")
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


def _resolve_pkg_share(package: str) -> str:
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
    # 1. _state.package_shares lookup.
    #    Preview: contains workspace source paths.
    #    Postbuild: contains install paths from AMENT_PREFIX_PATH.
    if package in _state.package_shares:
        pkg_path: str = _state.package_shares[package]
        # In preview mode, lockfile packages may need full fetch.
        if (
            _state.preview_mode
            and _state.lockfile_data
            and package in _state.lockfile_data
            and not os.path.isfile(os.path.join(pkg_path, "package.xml"))
        ):
            if _ensure_fetched(package):
                return str(_state.package_shares[package])
            _error(f"failed to fetch package '{package}' from lockfile")
            return f"$(find-pkg-share {package})"
        return pkg_path
    # 2. Lockfile package not yet in _state.package_shares — fetch (preview only).
    if _state.preview_mode and _state.lockfile_data and package in _state.lockfile_data:
        if _ensure_fetched(package):
            return str(_state.package_shares[package])
        _error(f"failed to fetch package '{package}' from lockfile")
        return f"$(find-pkg-share {package})"
    # 3. Non-lockfile packages (system / rosdep): use AMENT_PREFIX_PATH.
    if _real_get_package_share_directory is not None:
        try:
            return str(_real_get_package_share_directory(package))
        except Exception:
            pass  # Fall through to rosdep or portable fallback.
    # 4. Try rosdep install if enabled.
    if (
        _state.rosdep_fallback
        and _try_rosdep_install(package)
        and _real_get_package_share_directory is not None
    ):
        try:
            return str(_real_get_package_share_directory(package))
        except Exception:
            pass
    # 5. Fallback.
    if _state.preview_mode:
        # Preview: return portable syntax — downstream open() will hit the
        # FileNotFoundError stub which emits a warning.
        return f"$(find-pkg-share {package})"
    # Postbuild: package not installed — this is an error.
    raise LookupError(f"package '{package}' not found in AMENT_PREFIX_PATH")


def _resolve_ros_substitutions(value: str) -> str:
    """Replace $(find-pkg-share X) patterns in a string with real paths."""
    return re.sub(
        r"\$\(find-pkg-share ([^)]+)\)",
        lambda m: _resolve_pkg_share(m.group(1).strip()),
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


def _track_package(pkg):
    if not pkg:
        return
    # Skip substitution objects (e.g. LaunchConfiguration) — the variable name
    # is NOT a real package.  The resolved value will be tracked later in
    # _resolve_node_details / _resolve_composable_plugins.
    if _is_substitution(pkg):
        return
    pkg = str(pkg)
    if pkg and pkg not in _state.tracked["packages"]:
        _state.tracked["packages"].append(pkg)


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


def _track_include(path):
    if not path:
        return
    path = str(path)
    if path not in _state.tracked["includes"]:
        _state.tracked["includes"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {
            "package": dep[0],
            "share_path": dep[1],
            "path": path,
            "namespace_stack": list(_state.namespace_stack),
        }
        # Don't deduplicate: the same file may be included multiple times under
        # different <push-ros-namespace> contexts, and each entry carries a distinct
        # namespace_stack needed for correct namespace propagation.
        entry["include_args"] = {}
        _state.tracked["include_deps"].append(entry)
    # Return the index of the last entry with this path so callers can
    # attach include_args to the correct entry.
    for i in range(len(_state.tracked["include_deps"]) - 1, -1, -1):
        if _state.tracked["include_deps"][i].get("path") == path:
            return i
    return -1


def _track_param_file(path):
    if not path:
        return
    path = str(path)
    if path not in _state.tracked["param_files"]:
        _state.tracked["param_files"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {"package": dep[0], "share_path": dep[1]}
        if entry not in _state.tracked["param_file_deps"]:
            _state.tracked["param_file_deps"].append(entry)


def _current_source_key() -> str:
    """Return the source key for the current file being resolved.

    Uses the last entry of _state.include_chain, or _state.root_source_key for root-level.
    Format: "pkg://share_path".
    """
    if _state.include_chain:
        pkg, path = _state.include_chain[-1]
        return f"{pkg}://{path}" if pkg else path
    return _state.root_source_key


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


def _record_declared_arg(name: str, default: str, *, flat: bool = True) -> None:
    """Record a declared arg in the per-file dict, and optionally the flat list.

    The flat list is used for apply_arg_defaults (first-declaration wins).
    The per-file dict is used by --show-args to render arg comments per included file.
    """
    if flat:
        _state.tracked["declared_args"].append({"name": name, "default": default})
    key = _current_source_key()
    if key:
        by_file = _state.tracked["declared_args_by_file"]
        if key not in by_file:
            by_file[key] = []
        by_file[key].append({"name": name, "default": default})


def _track_node(node_dict: dict) -> int:
    """Append a node dict to _state.tracked["nodes"] with source info from _state.include_chain.

    Returns the index of the appended node.
    """
    if _state.include_chain:
        node_dict["include_chain"] = list(_state.include_chain)
    idx = len(_state.tracked["nodes"])
    _state.tracked["nodes"].append(node_dict)
    return idx


def _track_event_handler(eh_dict: dict) -> int:
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
    return _track_node(node_dict)


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


def _track_node_from_action(package, executable, name=None):
    """Track a node from an unpatched ROS 2 action (e.g. from OpaqueFunction return)."""
    # Pass raw package to _track_package BEFORE stringifying — _track_package
    # has an _is_substitution guard that filters out substitution objects.
    _track_package(package)
    package = str(package) if package else ""
    executable = str(executable) if executable else ""
    name = str(name) if name else ""
    return _track_node(
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
import builtins as _builtins
import io as _io
import pathlib as _pathlib

import launch_plus.entities.actions  # noqa: F401, E402
from launch_plus.entities.actions.arg import _apply_declared_arg, _DeclaredArg  # noqa: E402

# ─── Shim classes ─────────────────────────────────────────────────────────────
from launch_plus.entities.actions.base import _TrackedAction  # noqa: E402
from launch_plus.entities.actions.env import (  # noqa: E402
    _TrackedPushRosNamespace,
    _TrackedSetEnvironmentVariable,
    _TrackedUnsetEnvironmentVariable,
)
from launch_plus.entities.actions.event_handler import (  # noqa: E402
    _TrackedChangeState,
    _TrackedEmitEvent,
    _TrackedMatchesAction,
    _TrackedOnProcessExit,
    _TrackedOnProcessStart,
    _TrackedOnShutdown,
    _TrackedOnStateTransition,
    _TrackedRegisterEventHandler,
    _TrackedShutdown,
)
from launch_plus.entities.actions.executable import _TrackedExecutable  # noqa: E402
from launch_plus.entities.actions.group import (  # noqa: E402
    _TimerAction,
    _TrackedGroupAction,
    _TrackedOpaqueFunction,
)
from launch_plus.entities.actions.include import (  # noqa: E402
    _TrackedIncludeLaunchDescription,
)
from launch_plus.entities.actions.node import (  # noqa: E402
    _TrackedComposableNode,
    _TrackedComposableNodeContainer,
    _TrackedLifecycleNode,
    _TrackedLoadComposableNodes,
    _TrackedNode,
)
from launch_plus.entities.actions.param import (  # noqa: E402
    _SetLaunchConfiguration,
    _TrackedParameterFile,
    _TrackedSetParameter,
)
from launch_plus.entities.substitutions.find_pkg_share import _TrackedFindPackageShare  # noqa: E402
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

_STUB_ROS_PARAM_YAML = "/**:\n  ros__parameters: {}\n"


class _DefaultParamDict(dict):
    """Dict wrapper that returns a falsy, dict-like sentinel for missing keys.

    Used as a stand-in for a ROS 2 ``ros__parameters`` dict when the param
    file could not be read.

    Returning an empty ``_DefaultParamDict()`` (rather than ``False``) satisfies
    both common usage patterns:

    1. **Boolean guards** — ``if params["flag"]:`` evaluates to ``False``
       because an empty dict is falsy, so optional feature branches are skipped.
    2. **Dict operations** — code that calls ``.update()``, ``len()``, or
       iterates over a nested param block (e.g. ``fusion_config``) succeeds
       without raising ``AttributeError: 'bool' object has no attribute 'update'``.
    """

    def __missing__(self, key):
        return _DefaultParamDict()


def _call_opaque_with_stubs(fn, context):
    """Call ``fn(context)`` with file-not-found stubs active.

    In **preview mode**, patches ``builtins.open``, ``yaml.safe_load``, and
    ``os.path`` predicates for the duration of the call so that portable
    ``$(find-pkg-share pkg)/...`` paths are intercepted and resolved to actual
    filesystem paths via the ``_state.package_shares`` map.  If the target package
    exists in the lockfile but hasn't been fully fetched yet (no
    ``package.xml``), ``_ensure_fetched()`` is called inline.  If fetching fails,
    an error is logged and a stub/fallback is returned.

    In **non-preview mode** (post-build), all packages are installed and
    ``FindPackageShare`` returns real AMENT paths, so ``open()`` and
    ``os.path.*`` work natively.  No shimming is performed.
    """
    # Non-preview: packages are installed, paths are real — no shimming needed.
    if not _state.preview_mode:
        return fn(context)

    import yaml as _yaml

    _orig_open = _builtins.open
    _orig_safe_load = _yaml.safe_load
    _orig_path_exists = os.path.exists
    _orig_path_isfile = os.path.isfile
    _orig_path_isdir = os.path.isdir
    _orig_pathlib_open = _pathlib.Path.open

    def _resolve_portable(path_str: str):
        """Resolve a portable path to an actual filesystem path.

        Returns the resolved path string, or ``None`` if the input is not a
        portable path.  Fetches the package inline if needed via _ensure_fetched().
        Returns ``None`` if the package cannot be resolved.
        """
        parsed = _parse_portable_path(path_str)
        if parsed is None:
            return None
        pkg, rest = parsed
        if pkg in _state.package_shares:
            pkg_dir = _state.package_shares[pkg]
            if not _orig_path_isfile(os.path.join(pkg_dir, "package.xml")):
                if _ensure_fetched(pkg):
                    pkg_dir = _state.package_shares[pkg]
                else:
                    _error(f"failed to fetch package '{pkg}' from lockfile")
                    return None
            return os.path.join(pkg_dir, rest) if rest else pkg_dir
        # Try fetching if it's a lockfile package.
        if pkg in _state.lockfile_data and _ensure_fetched(pkg):
            pkg_dir = _state.package_shares[pkg]
            return os.path.join(pkg_dir, rest) if rest else pkg_dir
        # Try AMENT_PREFIX_PATH for packages not in the lockfile.
        if _real_get_package_share_directory is not None:
            try:
                share = _real_get_package_share_directory(pkg)
                return os.path.join(share, rest) if rest else share
            except Exception:
                pass
        # Package not found anywhere that we know about — can't resolve.
        return None

    def _stub_open(path, mode="r", *args, **kwargs):
        path_str = str(path)
        # Handle portable paths.
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction opened a portable path without --apply-opaque-file-access "
                    f"(stub returned): {path}"
                )
                return _io.StringIO(_STUB_ROS_PARAM_YAML)
            actual = _resolve_portable(path_str)
            if actual is not None:
                try:
                    return _orig_open(actual, mode, *args, **kwargs)
                except (FileNotFoundError, OSError):
                    _error(
                        f"param file not found: '{actual}' (resolved from '{path}') — stub defaults used"
                    )
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            _error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)
        # Regular (non-portable) path.
        try:
            return _orig_open(path, mode, *args, **kwargs)
        except (FileNotFoundError, OSError):
            # If the missing file is inside a lockfile package that hasn't been fully
            # fetched yet (no package.xml), try to fetch it inline.
            for pkg_name, pkg_dir in list(_state.package_shares.items()):
                pkg_dir_norm = pkg_dir.rstrip("/")
                if path_str.startswith(pkg_dir_norm + "/") or path_str.startswith(
                    pkg_dir_norm + os.sep
                ):
                    if not _orig_path_isfile(os.path.join(pkg_dir_norm, "package.xml")):
                        if _ensure_fetched(pkg_name):
                            # Retry open after fetching.
                            try:
                                return _orig_open(path, mode, *args, **kwargs)
                            except (FileNotFoundError, OSError):
                                pass  # File still missing after fetch → fall through to error
                        else:
                            _error(f"failed to fetch package '{pkg_name}' from lockfile")
                            break
                    break  # package is fully fetched; file genuinely missing → error
            _error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)

    def _stub_path_exists(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.exists on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)
            if actual is not None:
                return _orig_path_exists(actual)
            return False
        return _orig_path_exists(path)

    def _stub_path_isfile(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.isfile on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)
            if actual is not None:
                return _orig_path_isfile(actual)
            return False
        return _orig_path_isfile(path)

    def _stub_path_isdir(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.isdir on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)
            if actual is not None:
                return _orig_path_isdir(actual)
            return False
        return _orig_path_isdir(path)

    def _stub_pathlib_open(
        path_self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        """Intercept Path.open() so that Path(...).read_text() also handles portable paths.

        pathlib.Path.read_text() calls self.open() internally, bypassing builtins.open.
        This stub redirects the call to the resolved actual path when the Path object
        holds a portable $(find-pkg-share ...) string.
        """
        path_str = str(path_self)
        if _parse_portable_path(path_str) is not None:
            if not _state.apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called Path.open on a portable path without "
                    f"--apply-opaque-file-access (stub returned): {path_str}"
                )
                return _io.StringIO(_STUB_ROS_PARAM_YAML)
            actual = _resolve_portable(path_str)
            if actual is not None:
                try:
                    return _orig_pathlib_open(
                        _pathlib.Path(actual), mode, buffering, encoding, errors, newline
                    )
                except (FileNotFoundError, OSError):
                    _error(
                        f"param file not found: '{actual}' (resolved from '{path_str}') — stub defaults used"
                    )
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            _error(f"param file not found: '{path_str}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)
        return _orig_pathlib_open(path_self, mode, buffering, encoding, errors, newline)

    def _patched_safe_load(stream):
        result = _orig_safe_load(stream)
        # Wrap ros__parameters dicts in _DefaultParamDict so that missing
        # keys return False rather than raising KeyError.
        if isinstance(result, dict):
            for _ns_key, ns_val in result.items():
                if isinstance(ns_val, dict) and "ros__parameters" in ns_val:
                    rp = ns_val["ros__parameters"]
                    if isinstance(rp, dict):
                        ns_val["ros__parameters"] = _DefaultParamDict(rp)
        return result

    _builtins.open = _stub_open
    _yaml.safe_load = _patched_safe_load
    os.path.exists = _stub_path_exists
    os.path.isfile = _stub_path_isfile
    os.path.isdir = _stub_path_isdir
    _pathlib.Path.open = _stub_pathlib_open
    try:
        return fn(context)
    finally:
        _builtins.open = _orig_open
        _yaml.safe_load = _orig_safe_load
        os.path.exists = _orig_path_exists
        os.path.isfile = _orig_path_isfile
        os.path.isdir = _orig_path_isdir
        _pathlib.Path.open = _orig_pathlib_open


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


# ─── Node detail resolution helpers ──────────────────────────────────────────


def _env_overrides():
    """Return a copy of the current env overrides for per-node output."""
    return dict(_state.env)


def _resolve_node_details(node, context):
    """Fill in deferred details (package, executable, name, namespace, params, remaps, env).

    Works for both ``_TrackedNode`` / ``_TrackedLifecycleNode`` and
    ``_TrackedComposableNodeContainer`` — both expose the same raw fields.
    """
    entry = _state.tracked["nodes"][node._idx]

    # Package / executable / name: resolve substitutions (e.g. LaunchConfiguration)
    # that could not be resolved at construction time.
    for field_name in ("package", "executable", "name"):
        raw = getattr(node, f"_raw_{field_name}", None)
        if _is_substitution(raw):
            resolved, is_fallback = _resolve_substitution_ex(raw, context)
            if resolved is not None:
                entry[field_name] = resolved
                if field_name == "package" and not is_fallback:
                    _track_package(resolved)

    # Namespace: emit raw inputs — effective_namespace is computed downstream
    ns = (
        _resolve_substitution(node._raw_namespace, context)
        if node._raw_namespace is not None
        else None
    )
    entry["namespace_stack"] = list(_state.namespace_stack)
    entry["explicit_namespace"] = ns

    # Parameters and param files
    params = {}
    pf_list: list[dict] = []
    seen_pf: set[str] = set()
    for p in node._raw_parameters:
        if hasattr(p, "_param_file"):
            path = p._param_file
            # Deferred resolution: try again with live context if not resolved eagerly
            if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                raw = p._raw_param_file
                if hasattr(raw, "perform"):
                    try:
                        result = raw.perform(context)
                        if result is not None:
                            path = str(result)
                    except Exception:
                        pass
                elif not isinstance(raw, str):
                    path = str(raw)
            if path:
                path = str(path)
                if path not in seen_pf:
                    seen_pf.add(path)
                    pf_entry: dict = {"path": path}
                    if _state.inline_params:
                        expanded = _read_and_expand_param_file(path)
                        if expanded is not None:
                            pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
        elif isinstance(p, dict):
            for k, v in p.items():
                resolved_v = _resolve_substitution(v, context)
                params[str(k)] = resolved_v if resolved_v is not None else ""
    # Merge global params from the launch context (global first, node-local overrides).
    # In ROS 2, Node.execute() reads global_params from context._launch_configurations.
    # ComposableNodeContainer inherits from Node, so it also gets global params.
    ctx_global_params = context._launch_configurations.get("global_params", [])
    merged_params = {k: str(v) for k, v in ctx_global_params}
    merged_params.update(params)
    entry["parameters"] = merged_params
    entry["param_files"] = list(_state.global_param_files) + pf_list

    # Remappings: list of [src, dst] pairs — merge global remaps first
    remaps = list(_state.global_remaps)
    for r in node._raw_remappings:
        if isinstance(r, (tuple, list)) and len(r) == 2:
            src = _resolve_substitution(r[0], context)
            dst = _resolve_substitution(r[1], context)
            remaps.append([src or str(r[0]), dst or str(r[1])])
    entry["remappings"] = remaps

    # Env vars: start with inherited env diff, then node-local overrides
    env = _env_overrides()
    raw_env = node._raw_env
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            env[_resolve_substitution(k, context) or str(k)] = (
                _resolve_substitution(v, context) or ""
            )
    elif isinstance(raw_env, (list, tuple)):
        for item in raw_env:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                k_str = _resolve_substitution(item[0], context) or str(item[0])
                v_str = _resolve_substitution(item[1], context) or ""
                env[k_str] = v_str
    entry["env"] = env

    # output / arguments / respawn / respawn_delay
    for attr, key in (
        ("_raw_output", "output"),
        ("_raw_arguments", "args"),
        ("_raw_respawn", "respawn"),
        ("_raw_respawn_delay", "respawn_delay"),
    ):
        raw = getattr(node, attr, None)
        if raw is not None:
            resolved = _resolve_substitution(raw, context)
            if resolved is not None:
                entry[key] = resolved
            elif isinstance(raw, str):
                entry[key] = raw
            else:
                entry[key] = str(raw)


def _resolve_composable_plugins(descs, context):
    """Convert ``_TrackedComposableNode`` descriptions to serialisable plugin dicts."""
    plugins = []
    for desc in descs:
        if not isinstance(desc, _TrackedComposableNode):
            # Fallback for real/unknown description objects
            pkg = getattr(desc, "package", None) or getattr(desc, "_package", None)
            plugin = getattr(desc, "plugin", None) or getattr(desc, "_plugin", None)
            if pkg or plugin:
                plugins.append(
                    {
                        "package": str(pkg) if pkg else "",
                        "plugin": str(plugin) if plugin else "",
                        "name": None,
                        "parameters": {},
                        "remappings": [],
                    }
                )
            continue
        params = {}
        pf_list: list[dict] = []
        seen_pf: set[str] = set()
        for p in desc._raw_parameters:
            if hasattr(p, "_param_file"):
                path = p._param_file
                # Deferred resolution: try again with live context if not resolved eagerly
                if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                    raw = p._raw_param_file
                    if hasattr(raw, "perform"):
                        try:
                            result = raw.perform(context)
                            if result is not None:
                                path = str(result)
                        except Exception:
                            pass
                    elif not isinstance(raw, str):
                        path = str(raw)
                if path:
                    path = str(path)
                    if path not in seen_pf:
                        seen_pf.add(path)
                        pf_entry: dict = {"path": path}
                        if _state.inline_params:
                            expanded = _read_and_expand_param_file(path)
                            if expanded is not None:
                                pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = _resolve_substitution(v, context)
                    params[str(k)] = resolved_v if resolved_v is not None else ""
        remaps = []
        for r in desc._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = _resolve_substitution(r[0], context)
                dst = _resolve_substitution(r[1], context)
                remaps.append(
                    [
                        src if src is not None else str(r[0]),
                        dst if dst is not None else str(r[1]),
                    ]
                )
        # Resolve package/plugin/name substitutions with the live context
        pkg = desc._package
        if _is_substitution(desc._raw_package):
            resolved_pkg, is_fallback = _resolve_substitution_ex(desc._raw_package, context)
            if resolved_pkg is not None:
                pkg = resolved_pkg
                if not is_fallback:
                    _track_package(resolved_pkg)
        plg = desc._plugin
        if _is_substitution(desc._raw_plugin):
            resolved_plg = _resolve_substitution(desc._raw_plugin, context)
            if resolved_plg is not None:
                plg = resolved_plg
        nm = desc._name
        if _is_substitution(desc._raw_name):
            resolved_nm = _resolve_substitution(desc._raw_name, context)
            if resolved_nm is not None:
                nm = resolved_nm
        plugins.append(
            {
                "package": pkg,
                "plugin": plg,
                "name": nm or None,
                "parameters": params,
                "remappings": remaps,
                "param_files": pf_list,
            }
        )
    return plugins


# ─── Inline Python include resolution ─────────────────────────────────────────


def _inline_resolve_python_launch(launch_file, parent_context, child_args):
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
            pkg_share = _resolve_pkg_share(pkg)
        except Exception:
            return  # Package not available
        real_path = os.path.join(pkg_share, rest)

    if not os.path.isfile(real_path):
        return  # File not on disk

    try:
        spec = importlib.util.spec_from_file_location(
            f"_inline_launch_{_state.walk_depth}", real_path
        )
        if spec is None or spec.loader is None:
            _warn(f"cannot load included launch file: {real_path}")
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        _warn(f"failed to load included launch file {real_path}: {e}")
        return

    if not hasattr(mod, "generate_launch_description"):
        return

    # Save scoping state that must be restored after inline execution.
    # Nodes, includes, packages, params, etc. are KEPT — the resolver
    # handles all includes inline.
    saved_declared_arg_names = set(_state.declared_arg_names)
    saved_namespace_depth = len(_state.namespace_stack)
    saved_env = dict(_state.env)

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
            _warn(f"generate_launch_description() failed in {launch_file}: {e}")
            return

        entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

        # Apply child launch arguments: explicit include args override parent context.
        # In strict mode (global_arg_cascade=False), strip parent context so the
        # child only sees explicitly forwarded args + its own DeclareLaunchArgument.
        if not _state.global_arg_cascade:
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
        _walk_actions(entities, parent_context)
    finally:
        # Restore scoping state only — nodes, includes, packages, params, etc.
        # are intentionally kept (includes are resolved inline).
        _state.declared_arg_names.clear()
        _state.declared_arg_names.update(saved_declared_arg_names)
        del _state.namespace_stack[saved_namespace_depth:]
        _state.env.clear()
        _state.env.update(saved_env)

    # Restore child-only args that were not SetLaunchConfiguration'd —
    # child DeclareLaunchArgument defaults should NOT leak into the parent
    # scope, only SetLaunchConfiguration is a deliberate side-effect.
    # Keep keys that were either already in the parent or were set via
    # SetLaunchConfiguration.  Also preserve "global_params" — this is the
    # accumulation list for SetParameter, not a launch argument.
    set_configs = set(_state.tracked["set_launch_configurations"].keys())
    for k in list(parent_context._launch_configurations):
        if k not in saved_configs and k not in set_configs and k != "global_params":
            del parent_context._launch_configurations[k]


# ─── Action walker ────────────────────────────────────────────────────────────


def _walk_actions(actions, context):
    """Walk a list of actions, calling ``execute()`` on each.

    Tracked actions (subclasses of ``_TrackedAction``) implement the polymorphic
    ``execute(context)`` method.  Untracked actions (real ROS 2 objects from
    OpaqueFunction returns) are handled by ``_walk_untracked_action()``.
    """
    if actions is None:
        return
    _state.walk_depth += 1
    if _state.walk_depth > 20:
        _state.walk_depth -= 1
        _warn("Max include depth reached while walking Python launch description")
        return
    try:
        for action in actions:
            if action is None:
                continue
            try:
                if isinstance(action, _TrackedAction):
                    children = action.execute(context)
                    if children:
                        _walk_actions(children, context)
                else:
                    _walk_untracked_action(action, context)
            except Exception as e:
                _error(f"Error walking action {type(action).__name__}: {e}")
    finally:
        _state.walk_depth -= 1


def _walk_untracked_action(action, context):
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
                result = _call_opaque_with_stubs(fn, context)
                if result:
                    _walk_actions(result, context)
            except Exception as e:
                _error(f"OpaqueFunction failed: {e}")
        return

    # Walk nested actions/entities from generic action objects
    if hasattr(action, "entities"):
        try:
            _walk_actions(action.entities, context)
        except Exception as e:
            _warn(f"failed to walk {cls_name}.entities: {e}")
    if hasattr(action, "_actions"):
        try:
            _walk_actions(action._actions, context)
        except Exception as e:
            _warn(f"failed to walk {cls_name}._actions: {e}")

    # Real Node/LifecycleNode from launch_ros (unpatched, e.g. from OpaqueFunction return)
    if cls_name in ("Node", "LifecycleNode") and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        _track_node_from_action(pkg, exe)
    elif cls_name == "ComposableNodeContainer" and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        name = getattr(action, "_name", None)
        _track_node_from_action(pkg, exe, name)
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
                _track_include(loc)

    # Warn about action classes we do not recognise.
    if cls_name not in _KNOWN_UNTRACKED_CLASSES:
        _warn(
            f"Unrecognised action type '{cls_name}' — any nodes or includes it "
            f"declares may not appear in the resolved output"
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

# ─── Import system patcher ────────────────────────────────────────────────────

_PATCHED_MODULES = {}


def _build_patched_launch():
    """Stub for the top-level `launch` package (used when ROS 2 is not installed)."""
    mod = types.ModuleType("launch")
    mod.__path__ = []  # mark as package so submodule imports work
    mod.__package__ = "launch"

    class _LaunchDescription:
        def __init__(self, entities=None, **kwargs):
            self.entities = list(entities or [])

        def add_action(self, action):
            self.entities.append(action)

    mod.LaunchDescription = _LaunchDescription
    mod.LaunchContext = _StubLaunchContext
    return mod


def _build_patched_launch_ros():
    """Stub for the top-level `launch_ros` package."""
    mod = types.ModuleType("launch_ros")
    mod.__path__ = []
    mod.__package__ = "launch_ros"
    return mod


def _build_patched_launch_ros_actions():
    mod = types.ModuleType("launch_ros.actions")
    mod.Node = _TrackedNode
    mod.LifecycleNode = _TrackedLifecycleNode
    mod.ComposableNodeContainer = _TrackedComposableNodeContainer
    mod.LoadComposableNodes = _TrackedLoadComposableNodes
    mod.SetParameter = _TrackedSetParameter
    mod.SetRemap = lambda *a, **kw: None
    mod.PushRosNamespace = _TrackedPushRosNamespace
    mod.SetParametersCallback = lambda *a, **kw: None
    return mod


def _build_patched_launch_ros_utilities():
    mod = types.ModuleType("launch_ros.utilities")

    def make_namespace_absolute(namespace):
        """Ensure namespace starts with /."""
        ns = str(namespace) if namespace else ""
        if ns and not ns.startswith("/"):
            return "/" + ns
        return ns or "/"

    def prefix_namespace(namespace, name):
        """Prepend namespace to name."""
        ns = str(namespace).rstrip("/") if namespace else ""
        n = str(name).lstrip("/") if name else ""
        if not ns:
            return n
        if not n:
            return ns
        return ns + "/" + n

    mod.make_namespace_absolute = make_namespace_absolute
    mod.prefix_namespace = prefix_namespace
    mod.get_node_name_count = lambda *a, **kw: 0
    mod.evaluate_parameters = lambda *a, **kw: []
    mod.normalize_parameters = lambda *a, **kw: []
    mod.add_node_name_count_to_name = lambda name, **kw: name
    return mod


def _build_patched_launch_ros_descriptions():
    mod = types.ModuleType("launch_ros.descriptions")
    mod.ComposableNode = _TrackedComposableNode
    mod.ParameterFile = _TrackedParameterFile
    return mod


def _build_patched_launch_substitutions():
    """Always-stub: avoids recursion since we also patch top-level `launch`."""
    mod = types.ModuleType("launch.substitutions")
    mod.__path__ = []  # mark as package so submodule imports work
    mod.FindPackageShare = _TrackedFindPackageShare
    mod.PathJoinSubstitution = _TrackedPathJoinSubstitution
    mod.LaunchConfiguration = _LaunchConfiguration
    _SENTINEL = object()

    class _DeferredEnvironmentVariable:
        """Deferred substitution: reads _state.env at perform() time, not construction."""

        def __init__(self, name, **kw):
            self._name = name
            self._default = kw.get("default_value", _SENTINEL)

        def perform(self, context=None):
            # Resolve name to a concrete string via _to_str.
            name = _to_str(self._name, context) or ""
            # Look up in override env, then process env.
            if name in _state.env:
                return _state.env[name]
            if name in os.environ:
                return os.environ[name]
            # No match — use default if provided, otherwise error.
            if self._default is not _SENTINEL:
                return _to_str(self._default, context) or ""
            _error(f"EnvironmentVariable: '{name}' is not set and no default was provided")
            return ""

        def __str__(self):
            # Avoid calling perform() without context — return the raw name.
            return str(self._name) if self._name is not None else ""

    mod.EnvironmentVariable = _DeferredEnvironmentVariable
    mod.TextSubstitution = lambda text="", **kw: str(text)
    mod.PythonExpression = lambda expression=None, **kw: None
    mod.ThisLaunchFileDir = lambda: Path(__file__).parent
    return mod


def _build_patched_launch_substitutions_environment_variable():
    """Submodule stub for ``from launch.substitutions.environment_variable import ...``."""
    parent = sys.modules.get("launch.substitutions")
    if parent is None:
        parent = _build_patched_launch_substitutions()
    mod = types.ModuleType("launch.substitutions.environment_variable")
    mod.EnvironmentVariable = parent.EnvironmentVariable
    return mod


def _build_patched_launch_actions():
    """Always-stub: avoids recursion since we also patch top-level `launch`."""
    mod = types.ModuleType("launch.actions")
    mod.IncludeLaunchDescription = _TrackedIncludeLaunchDescription
    mod.DeclareLaunchArgument = _DeclaredArg
    mod.OpaqueFunction = _TrackedOpaqueFunction
    mod.GroupAction = _TrackedGroupAction
    mod.SetLaunchConfiguration = _SetLaunchConfiguration
    mod.LogInfo = lambda *a, **kw: None
    mod.TimerAction = _TimerAction
    mod.RegisterEventHandler = _TrackedRegisterEventHandler
    mod.EmitEvent = _TrackedEmitEvent
    mod.Shutdown = _TrackedShutdown
    mod.PushLaunchConfigurations = lambda *a, **kw: None
    mod.PopLaunchConfigurations = lambda *a, **kw: None
    mod.SetEnvironmentVariable = _TrackedSetEnvironmentVariable
    mod.UnsetEnvironmentVariable = _TrackedUnsetEnvironmentVariable
    mod.ExecuteProcess = _TrackedExecutable
    mod.ExecuteLocal = lambda *a, **kw: None
    mod.OnProcessExit = _TrackedOnProcessExit
    mod.OnProcessStart = _TrackedOnProcessStart
    return mod


def _build_patched_launch_event_handlers():
    """Tracked stubs for launch.event_handlers — record event handler structure."""
    mod = types.ModuleType("launch.event_handlers")
    mod.OnProcessExit = _TrackedOnProcessExit
    mod.OnProcessStart = _TrackedOnProcessStart
    mod.OnProcessIO = lambda *a, **kw: None
    mod.OnShutdown = _TrackedOnShutdown
    mod.OnStateTransition = _TrackedOnStateTransition
    mod.OnExecutionComplete = lambda *a, **kw: None
    return mod


def _build_patched_launch_conditions():
    mod = types.ModuleType("launch.conditions")

    class _IfCondition:
        """Stub for IfCondition that supports .evaluate(context)."""

        def __init__(self, condition=None, **kwargs):
            self._condition = condition

        def evaluate(self, context):
            try:
                if hasattr(self._condition, "perform"):
                    val = str(self._condition.perform(context)).lower()
                    return val in ("true", "1", "yes", "on")
                if self._condition is None:
                    return False
                return bool(self._condition)
            except Exception:
                return False

    class _UnlessCondition(_IfCondition):
        """Stub for UnlessCondition: evaluate() returns the logical inverse of IfCondition."""

        def evaluate(self, context):
            return not super().evaluate(context)

    class _LaunchConfigurationEquals:
        def __init__(self, name=None, expected_value=None, **kwargs):
            self._name = name
            self._expected = expected_value

        def evaluate(self, context):
            try:
                val = context._launch_configurations.get(str(self._name), "")
                return str(val) == str(self._expected)
            except Exception:
                return False

    class _LaunchConfigurationNotEquals(_LaunchConfigurationEquals):
        def evaluate(self, context):
            return not super().evaluate(context)

    mod.IfCondition = _IfCondition
    mod.UnlessCondition = _UnlessCondition
    mod.LaunchConfigurationEquals = _LaunchConfigurationEquals
    mod.LaunchConfigurationNotEquals = _LaunchConfigurationNotEquals
    return mod


def _build_patched_launch_launch_description_sources():
    mod = types.ModuleType("launch.launch_description_sources")

    class _PythonLaunchDescriptionSource:
        def __init__(self, location=None, **kwargs):
            # Resolve to a string path.  Location may be:
            #   - None
            #   - A substitution object with .perform()  (e.g. PathJoinSubstitution)
            #   - A list of strings/substitutions to concatenate
            #     (e.g. [FindPackageShare("pkg"), "/launch/file.py"])
            #   - A plain string
            if location is None:
                self._location = None
            elif hasattr(location, "perform"):
                try:
                    result = location.perform(_StubLaunchContext())
                    self._location = str(result) if result is not None else str(location)
                except Exception:
                    self._location = None
            elif isinstance(location, list):
                stub_ctx = _StubLaunchContext()
                parts = []
                for sub in location:
                    if hasattr(sub, "perform"):
                        try:
                            result = sub.perform(stub_ctx)
                            parts.append(str(result) if result is not None else str(sub))
                        except Exception:
                            parts.append(str(sub))
                    else:
                        parts.append(str(sub))
                self._location = "".join(parts)
            else:
                self._location = str(location)

    class _AnyLaunchDescriptionSource(_PythonLaunchDescriptionSource):
        pass

    mod.PythonLaunchDescriptionSource = _PythonLaunchDescriptionSource
    mod.AnyLaunchDescriptionSource = _AnyLaunchDescriptionSource
    return mod


def _build_patched_launch_ros_substitutions():
    """launch_ros.substitutions — provides FindPackageShare (same as launch.substitutions)."""
    mod = types.ModuleType("launch_ros.substitutions")
    mod.FindPackageShare = _TrackedFindPackageShare
    return mod


def _build_patched_launch_ros_parameter_descriptions():
    mod = types.ModuleType("launch_ros.parameter_descriptions")
    mod.ParameterFile = _TrackedParameterFile
    mod.ParameterDescription = lambda *a, **kw: None
    mod.ParameterValue = lambda *a, **kw: None
    return mod


def _build_patched_ament_index_python():
    mod = types.ModuleType("ament_index_python")
    mod.__path__ = []
    mod.__package__ = "ament_index_python"
    return mod


def _build_patched_ament_index_python_packages():
    mod = types.ModuleType("ament_index_python.packages")

    def get_package_share_directory(package_name):
        _warn(
            f"get_package_share_directory('{package_name}') is non-idiomatic; "
            f"prefer FindPackageShare('{package_name}') from launch_ros.substitutions"
        )
        _track_package(package_name)
        # Early fetch check using the actual package dir (before returning a portable path).
        # This ensures that if the package is in the lockfile but hasn't been fully fetched
        # yet (no package.xml), we fetch it inline — important for module-level callers
        # where os.path stubs may not be active.
        if package_name in _state.package_shares:
            pkg_dir = _state.package_shares[package_name]
            if not os.path.isfile(os.path.join(pkg_dir, "package.xml")) and not _ensure_fetched(
                package_name
            ):
                _error(f"failed to fetch package '{package_name}' from lockfile")
        elif package_name in _state.lockfile_data:
            if not _ensure_fetched(package_name):
                _error(f"failed to fetch package '{package_name}' from lockfile")
        # In non-preview mode, return the real install path so the output contains
        # absolute paths matching the installed layout.
        if not _state.preview_mode and package_name in _state.package_shares:
            return _state.package_shares[package_name]
        # In preview mode, return a portable path so that derived paths (e.g.
        # os.path.join(share_dir, "calib/")) stay portable.  Inside
        # _call_opaque_with_stubs the os.path.* stubs intercept any filesystem
        # access and resolve the portable path lazily.
        return f"$(find-pkg-share {package_name})"

    def get_package_prefix(package_name):
        # Return the parent of the share directory as a best-effort prefix.
        # For unresolved packages the share path is portable syntax like
        # "$(find-pkg-share pkg)" — fall back to the ROS distro prefix.
        share = _resolve_pkg_share(package_name)
        if share.startswith("$("):
            return _ROS_DISTRO_PREFIX
        p = Path(share)
        return str(p.parent) if p.parent != p else _ROS_DISTRO_PREFIX

    mod.get_package_share_directory = get_package_share_directory
    mod.get_package_prefix = get_package_prefix
    return mod


def _build_patched_launch_ros_events():
    """Patched launch_ros.events — provides ChangeState."""
    mod = types.ModuleType("launch_ros.events")
    mod.__path__ = []
    mod.__package__ = "launch_ros.events"
    mod.ChangeState = _TrackedChangeState
    return mod


def _build_patched_launch_ros_events_lifecycle():
    """Patched launch_ros.events.lifecycle — provides ChangeState."""
    mod = types.ModuleType("launch_ros.events.lifecycle")
    mod.ChangeState = _TrackedChangeState
    return mod


def _build_patched_launch_ros_event_handlers():
    """Patched launch_ros.event_handlers — lifecycle event matchers."""
    mod = types.ModuleType("launch_ros.event_handlers")
    mod.__path__ = []
    mod.__package__ = "launch_ros.event_handlers"
    mod.OnStateTransition = _TrackedOnStateTransition
    return mod


def _build_patched_launch_ros_event_handlers_on_state_transition():
    mod = types.ModuleType("launch_ros.event_handlers.on_state_transition")
    mod.OnStateTransition = _TrackedOnStateTransition
    return mod


def _build_patched_lifecycle_msgs():
    """Patched lifecycle_msgs — provides Transition constants."""
    mod = types.ModuleType("lifecycle_msgs")
    mod.__path__ = []
    mod.__package__ = "lifecycle_msgs"
    return mod


def _build_patched_lifecycle_msgs_msg():
    """Patched lifecycle_msgs.msg — provides Transition constants."""
    mod = types.ModuleType("lifecycle_msgs.msg")

    class Transition:
        TRANSITION_CONFIGURE = 1
        TRANSITION_CLEANUP = 2
        TRANSITION_ACTIVATE = 3
        TRANSITION_DEACTIVATE = 4
        TRANSITION_UNCONFIGURED_SHUTDOWN = 5
        TRANSITION_INACTIVE_SHUTDOWN = 6
        TRANSITION_ACTIVE_SHUTDOWN = 7

    mod.Transition = Transition
    return mod


def _build_patched_launch_events():
    """Patched launch.events — provides Shutdown event and matches_action."""
    mod = types.ModuleType("launch.events")
    mod.__path__ = []
    mod.__package__ = "launch.events"
    mod.Shutdown = _TrackedShutdown
    mod.matches_action = _TrackedMatchesAction
    return mod


def _build_patched_launch_events_process():
    mod = types.ModuleType("launch.events.process")
    return mod


def _build_patched_launch_event_handler():
    mod = types.ModuleType("launch.event_handler")
    mod.EventHandler = lambda *a, **kw: None
    return mod


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Intercept imports of specific launch/ament modules and return patched versions."""

    PATCHED = {
        "launch": _build_patched_launch,
        "launch_ros": _build_patched_launch_ros,
        "launch_ros.actions": _build_patched_launch_ros_actions,
        "launch_ros.descriptions": _build_patched_launch_ros_descriptions,
        "launch_ros.substitutions": _build_patched_launch_ros_substitutions,
        "launch_ros.parameter_descriptions": _build_patched_launch_ros_parameter_descriptions,
        "launch_ros.utilities": _build_patched_launch_ros_utilities,
        "launch.substitutions": _build_patched_launch_substitutions,
        "launch.substitutions.environment_variable": _build_patched_launch_substitutions_environment_variable,
        "launch.actions": _build_patched_launch_actions,
        "launch.event_handlers": _build_patched_launch_event_handlers,
        "launch.conditions": _build_patched_launch_conditions,
        "launch.launch_description_sources": _build_patched_launch_launch_description_sources,
        "ament_index_python": _build_patched_ament_index_python,
        "ament_index_python.packages": _build_patched_ament_index_python_packages,
        "launch_ros.events": _build_patched_launch_ros_events,
        "launch_ros.events.lifecycle": _build_patched_launch_ros_events_lifecycle,
        "launch_ros.event_handlers": _build_patched_launch_ros_event_handlers,
        "launch_ros.event_handlers.on_state_transition": _build_patched_launch_ros_event_handlers_on_state_transition,
        "lifecycle_msgs": _build_patched_lifecycle_msgs,
        "lifecycle_msgs.msg": _build_patched_lifecycle_msgs_msg,
        "launch.events": _build_patched_launch_events,
        "launch.events.process": _build_patched_launch_events_process,
        "launch.event_handler": _build_patched_launch_event_handler,
    }

    def find_spec(self, fullname, path, target=None):
        if fullname in self.PATCHED:
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        if spec.name not in _PATCHED_MODULES:
            _PATCHED_MODULES[spec.name] = self.PATCHED[spec.name]()
        return _PATCHED_MODULES[spec.name]

    def exec_module(self, module):
        pass  # Already fully initialised in create_module


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
    # ── Set up globals ─────────────────────────────────────────────────────

    launch_file_str = str(launch_file)

    _state.namespace_stack = []
    root_dep = _extract_pkg_and_share_path(launch_file_str)
    _state.root_source_key = f"{root_dep[0]}://{root_dep[1]}" if root_dep else launch_file_str

    _state.env.clear()
    _state.global_params.clear()
    _state.global_remaps.clear()
    _state.global_param_files.clear()
    _state.fetched_packages.clear()
    _state.declared_arg_names.clear()
    _state.include_chain.clear()
    _state.package_shares = dict(package_shares)

    # Reset _state.tracked
    _state.tracked["packages"] = []
    _state.tracked["includes"] = []
    _state.tracked["nodes"] = []
    _state.tracked["warnings"] = []
    _state.tracked["errors"] = []
    _state.tracked["declared_args"] = []
    _state.tracked["declared_args_by_file"] = {}
    _state.tracked["global_params"] = []
    _state.tracked["include_args"] = {}
    _state.tracked["param_files"] = []
    _state.tracked["set_launch_configurations"] = {}
    _state.tracked["include_deps"] = []
    _state.tracked["param_file_deps"] = []
    _state.tracked["event_handlers"] = []

    # Workflow flags
    if workflow_options is not None:
        _state.apply_opaque_file_access = bool(
            getattr(workflow_options, "apply_opaque_file_access", False)
        )
        _state.preview_mode = bool(getattr(workflow_options, "preview", True))
        _state.inline_params = bool(getattr(workflow_options, "inline_params", False))
        _state.rosdep_fallback = bool(getattr(workflow_options, "rosdep_fallback", False))
        _state.apply_arg_defaults = bool(getattr(workflow_options, "apply_arg_defaults", False))
        _state.global_arg_cascade = bool(getattr(workflow_options, "global_arg_cascade", False))
        _state.allow_unportable_paths = bool(
            getattr(workflow_options, "allow_unportable_paths", False)
        )
    else:
        _state.apply_opaque_file_access = False
        _state.preview_mode = True
        _state.inline_params = False
        _state.rosdep_fallback = False
        _state.apply_arg_defaults = False
        _state.global_arg_cascade = False
        _state.allow_unportable_paths = False

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
        _state.lockfile_data = lf_data
    else:
        _state.lockfile_data = {}

    _state.fetch_dir = str(fetch_dir) if fetch_dir else ""

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
            _error(f"cannot read {launch_file_str}: {e}")
            return _tracked_to_parsed_launch_file(_state.tracked)

        if launch_file_str.endswith((".yaml", ".yml")):
            elements = parse_yaml_launch(content, launch_file_str)
        else:
            elements = parse_xml_launch(content, launch_file_str)
        subst_ctx = _SubstitutionContext()
        subst_ctx.args = dict(args_dict)
        subst_ctx.launch_file_dir = os.path.dirname(os.path.abspath(launch_file_str))
        subst_ctx.preview_mode = _state.preview_mode
        subst_ctx.env = dict(_state.env)
        resolve_xml_elements(elements, subst_ctx, include_stack=[launch_file_str])
        return _tracked_to_parsed_launch_file(_state.tracked)

    # ── Python launch files ──────────────────────────────────────────────
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file_str)
    if spec is None or spec.loader is None:
        _error(f"cannot load {launch_file_str}")
        return _tracked_to_parsed_launch_file(_state.tracked)

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        _error(f"Error loading launch file: {e}")
        return _tracked_to_parsed_launch_file(_state.tracked)

    if not hasattr(mod, "generate_launch_description"):
        _error("No generate_launch_description() function found")
        return _tracked_to_parsed_launch_file(_state.tracked)

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
        _state.global_params.extend(gp_tuples)

    try:
        ld = mod.generate_launch_description()
    except Exception as e:
        _error(f"generate_launch_description() failed: {e}")
        return _tracked_to_parsed_launch_file(_state.tracked)

    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, _DeclaredArg):
            _apply_declared_arg(entity, ctx)

    _walk_actions(entities, ctx)

    return _tracked_to_parsed_launch_file(_state.tracked)


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
        warnings=list(tracked.get("warnings", [])),
        errors=list(tracked.get("errors", [])),
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
