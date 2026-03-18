#!/usr/bin/env python3
"""
launch-plus Python launch file resolver.

Executes a .launch.py file with import-patching hooks that intercept
launch_ros and launch classes to track package dependencies.

Usage:
    python3 py_resolver.py <launch_file> <args_json>

Output (JSON to stdout):
    {
        "packages": ["pkg_a", "pkg_b"],
        "includes": ["path/to/other.launch.py"],
        "nodes": [{"package": "...", "executable": "...", "name": "..."}],
        "warnings": ["..."]
    }
"""
import sys
import json
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import re
import types
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# ─── Output schema contract ───────────────────────────────────────────────────
# Defines the exact JSON schema emitted to stdout.  Rust's PyResolvedNode /
# PyResolverOutput structs must mirror these types.  Guarded by TYPE_CHECKING
# so there is zero runtime cost; mypy / pyright will enforce the contract.
if TYPE_CHECKING:
    from typing import TypedDict

    class _PluginDict(TypedDict):
        package: str
        plugin: str
        name: Optional[str]
        parameters: dict[str, str]
        remappings: list[list[str]]       # [[src, dst], ...]

    class _NodeDict(TypedDict):
        package: str
        executable: str
        name: str
        namespace_stack: list[str]        # PushRosNamespace stack at resolution time
        explicit_namespace: Optional[str]  # node's own namespace= kwarg, resolved
        parameters: dict[str, str]
        param_files: list[str]
        remappings: list[list[str]]        # [[src, dst], ...]
        env: dict[str, str]
        kind: str                          # "node" | "container" | "load_composable"
        plugins: list[_PluginDict]
        target: Optional[str]

    class _DeclaredArgDict(TypedDict):
        name: str
        default: str

    class _ResolverOutput(TypedDict):
        packages: list[str]
        includes: list[str]
        nodes: list[_NodeDict]
        warnings: list[str]
        errors: list[str]
        declared_args: list[_DeclaredArgDict]
        global_params: list[list]
        include_args: dict[str, dict[str, str]]
        param_files: list[str]
        set_launch_configurations: dict[str, str]

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

# ─── Workspace package share map ─────────────────────────────────────────────
# Filled in main() from sys.argv[3] (passed by orchestrator).
# Maps package_name → absolute path to the package root in the workspace src dir.
# Used to resolve $(find-pkg-share X) against source packages (not just installed ones).
_package_shares: dict = {}

# Lockfile packages whose source directories were not found on disk during this run.
# Populated by get_package_share_directory() when a lockfile package hasn't been cloned.
# Emitted in the JSON output so the Rust orchestrator can fetch them and retry py_resolver.
_packages_to_fetch: set = set()

# ─── Workflow flags ────────────────────────────────────────────────────────────
# Set from sys.argv[4] (JSON flags object) in main().
#
# When False (default), _stub_open and os.path stubs record an error and return
# stub data for portable-path access inside OpaqueFunction bodies.  When True,
# portable paths are resolved to actual filesystem paths (raising
# _PackageNotFetchedError when the package is not yet fully fetched).
#
# Exposed as --apply-opaque-file-access in the CLI.
_apply_opaque_file_access: bool = False

# ─── Preview mode flag ────────────────────────────────────────────────────────
# When True (default), FindPackageShare returns portable $(find-pkg-share pkg)
# tokens.  When False (non-preview / post-build), FindPackageShare returns the
# real path from _package_shares so the output contains absolute install paths.
_preview_mode: bool = True

# ─── Lockfile package names ────────────────────────────────────────────────────
# Filled in main() from the "lockfile_packages" key in the flags JSON (sys.argv[4]).
# When non-empty, _resolve_pkg_share() treats any package in this set as a
# source-only package: it never falls through to AMENT_PREFIX_PATH.  If the
# source directory is not fully fetched (no package.xml), _PackageNotFetchedError
# is raised instead, triggering the fetch-and-retry mechanism.
_lockfile_packages: set = set()


class _PackageNotFetchedError(RuntimeError):
    """Raised when a lockfile package's source directory doesn't exist on disk yet.

    Propagating this out of an OpaqueFunction body means the function cannot complete,
    but the failure is recoverable: Rust reads `packages_to_fetch` from the JSON output,
    clones the missing repos, and re-invokes py_resolver.
    """
    def __init__(self, pkg_name: str):
        super().__init__(f"package '{pkg_name}' source not on disk; needs fetch")
        self.pkg_name = pkg_name


# ─── Portable path support ────────────────────────────────────────────────────
#
# A "portable path" is a string in $(find-pkg-share <pkg>)/... format.  It is
# the canonical path representation used internally by launch-plus in all modes.
# Actual filesystem resolution is deferred to the two sites that require it:
#   1. Include expansion (Rust orchestrator resolves to read the included file).
#   2. File access inside OpaqueFunction bodies (via _stub_open / os.path stubs).
#
# This regex extracts the package name and the optional suffix from a portable path.

_PORTABLE_PATH_RE = re.compile(r'^\$\(find-pkg-share ([^)]+)\)(.*)')


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
    rest = m.group(2).lstrip('/').lstrip(os.sep)
    return pkg, rest


def _resolve_pkg_share(package: str) -> str:
    """Resolve a package share path.

    Lockfile packages (when _lockfile_packages is set) always resolve from the
    workspace source tree and NEVER fall through to AMENT_PREFIX_PATH.  If the
    source directory is not yet fully fetched (no package.xml), _PackageNotFetchedError
    is raised to trigger the fetch-and-retry mechanism in the Rust orchestrator.

    Non-lockfile packages (system / rosdep packages) are resolved via AMENT_PREFIX_PATH.
    """
    # 1. Workspace source packages (from orchestrator lockfile).
    if package in _package_shares:
        pkg_path = _package_shares[package]
        # For lockfile packages, enforce full fetch: if package.xml is absent the
        # package has been sparse-checked at the file level but not fully cloned.
        # Raise _PackageNotFetchedError rather than returning a dangling path.
        if _lockfile_packages and package in _lockfile_packages:
            if not os.path.isfile(os.path.join(pkg_path, "package.xml")):
                _packages_to_fetch.add(package)
                raise _PackageNotFetchedError(package)
        return pkg_path
    # 2. Lockfile package not found in _package_shares — shouldn't happen when the
    #    lockfile is configured, but guard against it: trigger fetch instead of
    #    silently falling through to AMENT_PREFIX_PATH with a potentially stale version.
    if _lockfile_packages and package in _lockfile_packages:
        _packages_to_fetch.add(package)
        raise _PackageNotFetchedError(package)
    # 3. Non-lockfile packages (system / rosdep): use AMENT_PREFIX_PATH.
    if _real_get_package_share_directory is not None:
        try:
            return _real_get_package_share_directory(package)
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"get_package_share_directory('{package}') failed: {e}")
    # 4. Fallback: return portable substitution syntax so that any downstream
    #    open() call hits the FileNotFoundError stub (which emits a warning) rather
    #    than silently using a wrong distro-specific path.
    return f"$(find-pkg-share {package})"


def _resolve_ros_substitutions(value: str) -> str:
    """Replace $(find-pkg-share X) patterns in a string with real paths."""
    return re.sub(
        r'\$\(find-pkg-share ([^)]+)\)',
        lambda m: _resolve_pkg_share(m.group(1).strip()),
        value,
    )

# ─── Result accumulator ──────────────────────────────────────────────────────

_tracked = {
    "packages": [],
    "includes": [],
    "nodes": [],
    "warnings": [],
    "errors": [],
    "declared_args": [],              # [{name, default}] — for Rust to forward based on apply_arg_defaults
    "global_params": [],              # [[name, value], ...] — SetParameter accumulations (real ROS 2 behavior)
    "include_args": {},               # {path: {arg: value}} — launch_arguments captured from IncludeLaunchDescription
    "param_files": [],                # [path_string, ...] — ParameterFile paths referenced by nodes
    "set_launch_configurations": {},  # {name: value} — SetLaunchConfiguration calls
    "include_deps": [],               # [{package, share_path}] — structured include dependencies
    "param_file_deps": [],            # [{package, share_path}] — structured param file dependencies
    "event_handlers": [],              # [{handler_kind, target, target_node, start_state, goal_state, actions}]
}

# Names of args already recorded in declared_args (first declaration wins).
_declared_arg_names: set = set()

# Stack of ROS namespaces pushed by PushRosNamespace actions.
# Each entry is a resolved string.  Managed by _walk_action; reset in main().
_namespace_stack: list = []

# Environment variable overrides.  Starts empty; mutated by
# SetEnvironmentVariable / UnsetEnvironmentVariable.  EnvironmentVariable
# substitution checks this first, then falls back to os.environ.
# Scoped groups clone/restore this.
_env: dict = {}

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
    if pkg and pkg not in _tracked["packages"]:
        _tracked["packages"].append(pkg)

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
        after_share = path_str[idx + 7:]  # skip "/share/"
        slash = after_share.find("/")
        if slash > 0:
            pkg = after_share[:slash]
            rest = after_share[slash + 1:]
            if rest:
                return pkg, rest
    return None


def _track_include(path):
    if not path:
        return
    path = str(path)
    if path not in _tracked["includes"]:
        _tracked["includes"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {
            "package": dep[0],
            "share_path": dep[1],
            "path": path,
            "namespace_stack": list(_namespace_stack),
        }
        # Don't deduplicate: the same file may be included multiple times under
        # different <push-ros-namespace> contexts, and each entry carries a distinct
        # namespace_stack that the orchestrator needs for correct namespace propagation.
        entry["include_args"] = {}
        _tracked["include_deps"].append(entry)
    # Return the index of the last entry with this path so callers can
    # attach include_args to the correct entry.
    for i in range(len(_tracked["include_deps"]) - 1, -1, -1):
        if _tracked["include_deps"][i].get("path") == path:
            return i
    return -1

def _track_param_file(path):
    if not path:
        return
    path = str(path)
    if path not in _tracked["param_files"]:
        _tracked["param_files"].append(path)
    dep = _extract_pkg_and_share_path(path)
    if dep:
        entry = {"package": dep[0], "share_path": dep[1]}
        if entry not in _tracked["param_file_deps"]:
            _tracked["param_file_deps"].append(entry)


def _to_str(value, context=None):
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
        except _PackageNotFetchedError:
            raise
        except Exception:
            return str(value)
    return str(value)


def _resolve_substitution(sub, context):
    """Resolve a substitution, list-of-substitutions, or plain string to str."""
    value, _fallback = _resolve_substitution_ex(sub, context)
    return value


def _resolve_substitution_ex(sub, context):
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
                    except _PackageNotFetchedError:
                        raise
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
        except _PackageNotFetchedError:
            raise
        except Exception:
            return str(sub), True
    return str(sub), True


def _track_node(package, executable, name=None):
    # Pass raw package to _track_package BEFORE stringifying — _track_package
    # has an _is_substitution guard that filters out substitution objects.
    _track_package(package)
    package = str(package) if package else ""
    executable = str(executable) if executable else ""
    name = str(name) if name else ""
    _tracked["nodes"].append({
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
    })
    return len(_tracked["nodes"]) - 1

def _warn(msg):
    _tracked["warnings"].append(msg)

def _error(msg):
    _tracked["errors"].append(msg)

# ─── Shim classes ─────────────────────────────────────────────────────────────

class _TrackedNode:
    def __init__(self, *, package=None, executable=None, name=None, **kwargs):
        _track_package(package)
        self._idx = len(_tracked["nodes"])
        _tracked["nodes"].append({
            "package": str(package) if package else "",
            "executable": str(executable) if executable else "",
            "name": str(name) if name else "",
            "namespace_stack": [],
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "node",
            "plugins": [],
            "target": None,
        })
        # Save raw kwargs for deferred resolution in _walk_action
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._detailed = False
        # Eager: track any ParameterFile paths identifiable at construction time
        for p in self._raw_parameters:
            if hasattr(p, "_param_file") and p._param_file:
                _track_param_file(p._param_file)

    def __repr__(self):
        return f"TrackedNode(package={_tracked['nodes'][self._idx]['package']!r})"

class _TrackedLifecycleNode(_TrackedNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        _tracked["nodes"][self._idx]["kind"] = "lifecycle_node"


class _TrackedEmitEvent:
    """Tracked emit_event — records the event type and optional target node."""
    def __init__(self, event=None, **kwargs):
        self._event = event
        self._target_node = None
        self._namespace_stack = []
        self._explicit_namespace = None
        # Extract target node from ChangeState-like event objects
        if event is not None and hasattr(event, "_target_node"):
            self._target_node = event._target_node
        if event is not None and hasattr(event, "_event_name"):
            self._event = event._event_name
        if event is not None and hasattr(event, "_namespace_stack"):
            self._namespace_stack = event._namespace_stack
            self._explicit_namespace = event._explicit_namespace

    def to_dict(self):
        return {
            "event": str(self._event) if self._event else "",
            "target_node": self._target_node,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
        }


class _TrackedChangeState:
    """Tracked ChangeState event — records the transition and target node.

    Real API: ``ChangeState(lifecycle_node_matcher=matches_action(node), transition_id=...)``
    where ``matches_action(node)`` returns a ``_TrackedMatchesAction`` that carries ``_node_name``.
    """
    def __init__(self, lifecycle_node_matcher=None, transition_id=None, **kwargs):
        self._event_name = _transition_name(transition_id)
        self._target_node = None
        self._namespace_stack = []
        self._explicit_namespace = None
        if lifecycle_node_matcher is not None and hasattr(lifecycle_node_matcher, "_node_name"):
            self._target_node = lifecycle_node_matcher._node_name
            self._namespace_stack = getattr(lifecycle_node_matcher, "_namespace_stack", [])
            self._explicit_namespace = getattr(lifecycle_node_matcher, "_explicit_namespace", None)


class _TrackedShutdown:
    """Tracked Shutdown event.

    When used directly as an action (rather than wrapped in EmitEvent),
    serializes as an emit_event with event="shutdown".
    """
    _event_name = "shutdown"

    def __init__(self, **kwargs):
        if kwargs:
            _error(
                f"Shutdown event arguments are not yet supported: "
                f"{', '.join(f'{k}={v!r}' for k, v in kwargs.items())}"
            )

    def to_dict(self):
        return {
            "event": "shutdown",
            "target_node": None,
            "namespace_stack": [],
            "explicit_namespace": None,
        }


class _TrackedMatchesAction:
    """Wraps ``matches_action(node)`` — carries the node name for ChangeState targeting.

    The real ``launch.events.matches_action`` returns a callable matcher.
    Our version also records the node name and namespace so ``_TrackedChangeState``
    can extract them.
    """
    def __init__(self, action):
        self._node_name = _action_name(action)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(action)

    def __call__(self, *args, **kwargs):
        return True  # dummy matcher


def _transition_name(transition_id):
    """Convert a lifecycle Transition constant to a human-readable name."""
    # Transition IDs are integers in the lifecycle_msgs
    _TRANSITION_MAP = {
        1: "configure",    # TRANSITION_CONFIGURE
        2: "cleanup",      # TRANSITION_CLEANUP
        3: "activate",     # TRANSITION_ACTIVATE
        4: "deactivate",   # TRANSITION_DEACTIVATE
        5: "shutdown",     # TRANSITION_UNCONFIGURED_SHUTDOWN
        6: "shutdown",     # TRANSITION_INACTIVE_SHUTDOWN
        7: "shutdown",     # TRANSITION_ACTIVE_SHUTDOWN
    }
    if isinstance(transition_id, int):
        return _TRANSITION_MAP.get(transition_id, f"transition_{transition_id}")
    return str(transition_id) if transition_id else ""


class _TrackedOnProcessStart:
    """Tracked OnProcessStart event handler."""
    def __init__(self, target_action=None, on_start=None, **kwargs):
        self._target_name = _action_name(target_action)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(target_action)
        self._actions = on_start or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_process_start",
            "target": self._target_name,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnProcessExit:
    """Tracked OnProcessExit event handler."""
    def __init__(self, target_action=None, on_exit=None, **kwargs):
        self._target_name = _action_name(target_action)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(target_action)
        self._actions = on_exit or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_process_exit",
            "target": self._target_name,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnStateTransition:
    """Tracked OnStateTransition event handler.

    Real API: ``OnStateTransition(target_lifecycle_node=node,
    start_state='configuring', goal_state='inactive', entities=[...])``
    """
    def __init__(self, target_lifecycle_node=None, start_state=None, goal_state=None,
                 entities=None, **kwargs):
        self._target_node = _action_name(target_lifecycle_node)
        self._namespace_stack, self._explicit_namespace = _action_namespace_info(target_lifecycle_node)
        self._start_state = str(start_state) if start_state else None
        self._goal_state = str(goal_state) if goal_state else None
        self._actions = entities or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_state_transition",
            "target": None,
            "target_node": self._target_node or None,
            "start_state": self._start_state,
            "goal_state": self._goal_state,
            "namespace_stack": self._namespace_stack,
            "explicit_namespace": self._explicit_namespace,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedOnShutdown:
    """Tracked OnShutdown event handler.

    OnShutdown fires when the launch system is shutting down.  It has no
    target process — it applies system-wide.  Rendered as ``<on_shutdown>``.
    """
    def __init__(self, on_shutdown=None, **kwargs):
        self._actions = on_shutdown or []

    def to_event_handler(self):
        return {
            "handler_kind": "on_shutdown",
            "target": None,
            "target_node": None,
            "start_state": None,
            "goal_state": None,
            "namespace_stack": [],
            "explicit_namespace": None,
            "actions": [a.to_dict() for a in self._actions if hasattr(a, "to_dict")],
        }


class _TrackedRegisterEventHandler:
    """Tracked RegisterEventHandler — records the event handler to _tracked."""
    def __init__(self, event_handler=None, **kwargs):
        if event_handler is not None and hasattr(event_handler, "to_event_handler"):
            _tracked["event_handlers"].append(event_handler.to_event_handler())


def _action_name(action):
    """Extract the node name from a tracked action for event handler targeting."""
    if action is not None and hasattr(action, "_idx"):
        return _tracked["nodes"][action._idx].get("name", "")
    return ""


def _action_namespace_info(action):
    """Extract namespace_stack and explicit_namespace for a tracked node action.

    Returns (namespace_stack, explicit_namespace) from the tracked entry.
    By the time event handlers are registered, _resolve_node_details has
    already run on the target node (LifecycleNode is processed before
    RegisterEventHandler in _walk_actions), so these fields are populated.
    """
    if action is None or not hasattr(action, "_idx"):
        return [], None
    entry = _tracked["nodes"][action._idx]
    return entry.get("namespace_stack", []), entry.get("explicit_namespace")


class _TrackedComposableNode:
    """A composable node plugin loaded into a container process.

    Does NOT add to the flat ``_tracked["nodes"]`` list — it is attached to the
    container's ``plugins`` list when the container is resolved in ``_walk_action``.
    """
    def __init__(self, *, package=None, plugin=None, name=None, **kwargs):
        _track_package(package)
        self._raw_package = package
        self._package = str(package) if package else ""
        self._raw_plugin = plugin
        self._plugin = str(plugin) if plugin else ""
        self._raw_name = name
        self._name = str(name) if name else ""
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        # Eager: track any ParameterFile paths
        for p in self._raw_parameters:
            if hasattr(p, "_param_file") and p._param_file:
                _track_param_file(p._param_file)

    def __repr__(self):
        return f"TrackedComposableNode(package={self._package!r}, plugin={self._plugin!r})"

class _TrackedComposableNodeContainer:
    """A composable node container process.

    Emits a ``kind='container'`` entry whose ``plugins`` list is populated during
    deferred resolution in ``_walk_action`` from the *composable_node_descriptions*.
    """
    def __init__(self, *, package=None, executable=None, name=None,
                 composable_node_descriptions=None, **kwargs):
        _track_package(package)
        self._idx = len(_tracked["nodes"])
        _tracked["nodes"].append({
            "package": str(package) if package else "",
            "executable": str(executable) if executable else "",
            "name": str(name) if name else "",
            "namespace_stack": [],
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "container",
            "plugins": [],
            "target": None,
        })
        self._raw_package = package
        self._raw_executable = executable
        self._raw_name = name
        self._raw_namespace = kwargs.get("namespace")
        self._raw_parameters = list(kwargs.get("parameters") or [])
        self._raw_remappings = list(kwargs.get("remappings") or [])
        self._raw_env = kwargs.get("env") or []
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False
        # Eager: track packages from descriptions — pass raw object so
        # _track_package can filter out substitution objects.
        for desc in self._descs:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                _track_package(raw_pkg)

class _TrackedLoadComposableNodes:
    """Loads composable nodes into an existing container.

    Emits a ``kind='load_composable'`` entry with ``target`` set to the container
    name and ``plugins`` populated during deferred resolution in ``_walk_action``.
    """
    def __init__(self, *, composable_node_descriptions=None, target_container=None, **kwargs):
        # Eagerly resolve target_container when it's a plain string
        if target_container is None:
            target_str = ""
        elif isinstance(target_container, str):
            target_str = target_container
        elif isinstance(target_container, _TrackedComposableNodeContainer):
            # The container object itself was passed directly — retrieve its registered name.
            target_str = _tracked["nodes"][target_container._idx].get("name", "")
        elif hasattr(target_container, "perform"):
            try:
                result = target_container.perform(_StubLaunchContext())
                target_str = str(result) if result is not None else str(target_container)
            except Exception:
                target_str = str(target_container)
        else:
            target_str = str(target_container)
        self._idx = len(_tracked["nodes"])
        self._raw_target = target_container
        _tracked["nodes"].append({
            "package": "",
            "executable": "",
            "name": "",
            "namespace_stack": [],
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "load_composable",
            "plugins": [],
            "target": target_str,
        })
        self._descs = list(composable_node_descriptions or [])
        self._detailed = False
        # Eager: track packages from descriptions — pass raw object so
        # _track_package can filter out substitution objects.
        for desc in self._descs:
            raw_pkg = getattr(desc, "_raw_package", None) or getattr(desc, "_package", None)
            if raw_pkg:
                _track_package(raw_pkg)

class _TrackedPushRosNamespace:
    """Tracks PushRosNamespace so _walk_action can update _namespace_stack."""
    def __init__(self, namespace=None, **kwargs):
        self._namespace = namespace  # string or substitution object


class _TrackedParameterFile:
    """Tracks ParameterFile references so they can be reported as param_file dependencies."""
    def __init__(self, param_file=None, *args, allow_substs=False, **kwargs):
        # param_file may be positional or keyword, and may be a string or substitution
        if param_file is None and args:
            param_file = args[0]
        self._param_file = None
        if param_file is not None:
            if isinstance(param_file, str):
                path = param_file  # Keep portable; Rust handles $(find-pkg-share ...) format
            elif hasattr(param_file, "perform"):
                try:
                    result = param_file.perform(_StubLaunchContext())
                    path = str(result) if result is not None else str(param_file)
                except Exception:
                    path = str(param_file)
            else:
                path = str(param_file) if param_file is not None else None
            if path:
                self._param_file = path
                _track_param_file(path)

class _SetLaunchConfiguration:
    """Implements SetLaunchConfiguration: updates launch_configurations at walk time."""
    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value

class _TimerAction:
    """Stores TimerAction child actions so the walker can recurse into them."""
    def __init__(self, *, period=None, actions=None, **kwargs):
        self._actions = list(actions or [])

class _TrackedFindPackageShare:
    """Tracks FindPackageShare; package may be a string or a list of substitutions."""
    def __init__(self, package):
        self._package_subs = package
        # Track statically when the name is a plain string
        if isinstance(package, str):
            _track_package(package)

    def _resolve_name(self, context=None):
        """Concatenate package name from string or list of substitution objects.

        Returns ``(name, is_fallback)`` where *is_fallback* is ``True`` when
        any part could not be resolved and the display name was used instead.
        """
        subs = self._package_subs
        if isinstance(subs, str):
            return subs, False
        if isinstance(subs, list):
            parts = []
            any_fallback = False
            for sub in subs:
                if context is not None and hasattr(sub, "perform"):
                    result = sub.perform(context)
                    if result is not None:
                        parts.append(str(result))
                    else:
                        parts.append(str(sub))
                        any_fallback = True
                else:
                    parts.append(str(sub))
                    if hasattr(sub, "perform"):
                        any_fallback = True
            return "".join(parts), any_fallback
        if hasattr(subs, "perform"):
            return str(subs), True
        return str(subs), False

    def perform(self, context):
        pkg, is_fallback = self._resolve_name(context)
        if not is_fallback:
            _track_package(pkg)
        if not _preview_mode and pkg in _package_shares:
            return _package_shares[pkg]
        return f"$(find-pkg-share {pkg})"

    def __str__(self):
        pkg, is_fallback = self._resolve_name(None)
        if not _preview_mode and pkg in _package_shares:
            return _package_shares[pkg]
        return f"$(find-pkg-share {pkg})"

class _TrackedPathJoinSubstitution:
    def __init__(self, substitutions):
        self._subs = substitutions
        # Track packages from nested FindPackageShare
        for sub in substitutions:
            if isinstance(sub, _TrackedFindPackageShare):
                pkg, is_fallback = sub._resolve_name()
                if not is_fallback:
                    _track_package(pkg)
    def perform(self, context):
        parts = []
        for sub in self._subs:
            if hasattr(sub, "perform"):
                result = sub.perform(context)
                parts.append(str(result) if result is not None else str(sub))
            else:
                parts.append(str(sub))
        return str(Path(*parts))

def _resolve_include_args(path, launch_arguments, context, dep_idx=-1):
    """Capture launch_arguments for an include site.

    When dep_idx >= 0, stores args directly into the include_deps entry
    (allowing distinct args per include site).  Falls back to the shared
    include_args dict for backward compatibility.

    Called both at construction time (with a stub context) and deferred during
    _walk_action (with the live context, which can resolve LaunchConfiguration
    and PathJoinSubstitution values correctly).  A second call for the same
    entry is skipped — the first resolved value wins.
    """
    if not launch_arguments or not path:
        return
    path = str(path)
    # Check if this entry already has args resolved.
    if dep_idx >= 0 and _tracked["include_deps"][dep_idx].get("include_args"):
        return
    if dep_idx < 0 and path in _tracked["include_args"]:
        return
    try:
        captured = {}
        for k, v in launch_arguments:
            k_str = str(k)
            if isinstance(v, list):
                parts = []
                for sub in v:
                    if hasattr(sub, "perform"):
                        try:
                            result = sub.perform(context)
                            parts.append(str(result) if result is not None else str(sub))
                        except _PackageNotFetchedError:
                            raise
                        except Exception:
                            parts.append(str(sub))
                    else:
                        parts.append(str(sub))
                v_str = "".join(parts)
            elif hasattr(v, "perform"):
                try:
                    result = v.perform(context)
                    v_str = str(result) if result is not None else str(v)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    v_str = str(v)
            else:
                v_str = str(v)
            captured[k_str] = v_str
        if captured:
            if dep_idx >= 0:
                _tracked["include_deps"][dep_idx]["include_args"] = captured
            else:
                _tracked["include_args"][path] = captured
    except _PackageNotFetchedError:
        raise
    except Exception as e:
        _warn(f"failed to resolve include args for '{path}': {e}")


class _TrackedIncludeLaunchDescription:
    def __init__(self, launch_description_source, launch_arguments=None, **kwargs):
        self._source = launch_description_source
        # Save raw args for deferred resolution in _walk_action (needed when the source
        # is a substitution like PathJoinSubstitution that can only be resolved with a
        # live LaunchContext — typically when constructed inside an OpaqueFunction).
        self._raw_launch_arguments = launch_arguments
        # Extract path from the source
        path = None
        if hasattr(launch_description_source, "_location"):
            path = launch_description_source._location
        elif hasattr(launch_description_source, "location"):
            try:
                path = launch_description_source.location
            except Exception:
                pass
        self._path = path
        self._dep_idx = -1
        if path:
            self._dep_idx = _track_include(path)

        # Capture launch_arguments so the orchestrator can forward them when it recursively
        # invokes py_resolver for the included file.  Values may be strings, lists of
        # substitution objects (common when constructed inside OpaqueFunction where pkg paths
        # are already resolved), or substitution objects; resolve eagerly where possible.
        if launch_arguments and path:
            _resolve_include_args(path, launch_arguments, _StubLaunchContext(), self._dep_idx)

class _LaunchConfiguration:
    """Substitution that resolves to a launch configuration value at runtime."""
    def __init__(self, variable_name, default=None, **kwargs):
        self._name = variable_name
        self._default = default
    def perform(self, context):
        if context and hasattr(context, "_launch_configurations"):
            if self._name in context._launch_configurations:
                return context._launch_configurations[self._name]
            if self._default is not None:
                return str(self._default)
        return None
    def __str__(self):
        return self._name

class _DeclaredArg:
    """Stub for DeclareLaunchArgument: captures name, default_value, and condition."""
    def __init__(self, name=None, *positional, default_value=None, condition=None, **kwargs):
        # ROS 2 signature: DeclareLaunchArgument(name, *, default_value=None, ...)
        # `name` may be passed positionally or as keyword.
        self.name = str(name) if name is not None else (
            str(positional[0]) if positional else None
        )
        self.default_value = default_value
        self.condition = condition


def _apply_declared_arg(arg: "_DeclaredArg", context) -> None:
    """Resolve a DeclareLaunchArgument default and apply it to the launch context.

    Two effects:
      1. Sets context._launch_configurations[name] if the arg is not already provided
         (CLI/parent args take precedence), making the default available to OpaqueFunction.
      2. Records the resolved default in _tracked["declared_args"] so the Rust orchestrator
         can forward it to child launch files according to the apply_arg_defaults flag.

    Processing order mirrors the XML resolver: each declaration is resolved immediately
    so that a later DeclareLaunchArgument can reference an earlier one via
    LaunchConfiguration("prev_arg").
    """
    if not arg.name:
        return

    # Evaluate condition if present — skip if the condition evaluates to False.
    if arg.condition is not None and hasattr(arg.condition, "evaluate"):
        try:
            if not arg.condition.evaluate(context):
                return
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"condition on DeclareLaunchArgument '{arg.name}' failed to evaluate: {e}; "
                  f"assuming condition is satisfied")

    if arg.default_value is None:
        return  # Required arg with no default — nothing to apply

    # Resolve the default_value, which may be a plain string, a substitution object,
    # or a list of substitution objects to be concatenated.
    try:
        dv = arg.default_value
        if hasattr(dv, "perform"):
            result = dv.perform(context)
            resolved = str(result) if result is not None else str(dv)
        elif isinstance(dv, list):
            parts = []
            for sub in dv:
                if hasattr(sub, "perform"):
                    result = sub.perform(context)
                    parts.append(str(result) if result is not None else str(sub))
                else:
                    parts.append(str(sub))
            resolved = "".join(parts)
        else:
            resolved = str(dv)
    except Exception as e:
        _warn(f"DeclareLaunchArgument('{arg.name}') default resolution failed: {e}")
        return

    # Record for the Rust orchestrator (first declaration wins; prevents duplicate entries
    # when the same arg is declared in multiple conditional branches).
    if arg.name not in _declared_arg_names:
        _declared_arg_names.add(arg.name)
        _tracked["declared_args"].append({"name": arg.name, "default": resolved})

    # Apply to the launch context only if the arg was not already set by the
    # CLI/parent chain — those values always take precedence.
    if context is not None and arg.name not in context._launch_configurations:
        context._launch_configurations[arg.name] = resolved


class _TrackedOpaqueFunction:
    """Stores an OpaqueFunction's callable so the walker can invoke it."""
    def __init__(self, *, function=None, **kwargs):
        self.function = function

class _TrackedGroupAction:
    """Stores GroupAction's child actions so the walker can recurse into them."""
    def __init__(self, actions=None, **kwargs):
        self._actions = list(actions or [])
        self._scoped = kwargs.get("scoped", True)
        self._condition = kwargs.get("condition")

class _TrackedSetEnvironmentVariable:
    """Tracks SetEnvironmentVariable: mutates _env in _walk_action."""
    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value
        self._condition = kwargs.get("condition")

class _TrackedUnsetEnvironmentVariable:
    """Tracks UnsetEnvironmentVariable: removes from _env in _walk_action."""
    def __init__(self, name=None, **kwargs):
        self._name = name
        self._condition = kwargs.get("condition")

class _TrackedSetParameter:
    """Mirrors launch_ros SetParameter: accumulates (name, value) into context['global_params'].

    In the real ROS 2 launch system, SetParameter.execute() appends (name, ParameterValue)
    tuples to context.launch_configurations['global_params'].  Child launch files (e.g.
    ground_segmentation.launch.py) read this list to access vehicle dimensions and similar
    cross-file global state.  We reproduce this here so the walker can populate the stub
    context and the orchestrator can persist the params across isolated py_resolver calls.
    """
    def __init__(self, name=None, value=None, **kwargs):
        # Keep raw for deferred resolution in _walk_action (value may be a substitution).
        self._name = name
        self._value = value
        self._idx = len(_tracked["nodes"])
        _tracked["nodes"].append({
            "package": "",
            "executable": "",
            "name": str(name) if name is not None and not hasattr(name, "perform") else "",
            "namespace_stack": [],
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "set_parameter",
            "plugins": [],
            "target": None,
            "param_value": "",
        })


class _TrackedExecutable:
    """Tracks an ExecuteProcess so the walker can render it as <executable>."""
    def __init__(self, *, cmd=None, name=None, shell=False, **kwargs):
        # `cmd` may be a list of substitution-bearing strings or a plain string.
        if isinstance(cmd, list):
            self._cmd = cmd
        elif cmd is not None:
            self._cmd = [cmd]
        else:
            self._cmd = []
        self._name = name
        self._shell = bool(shell)
        self._idx = len(_tracked["nodes"])
        _tracked["nodes"].append({
            "package": "",
            "executable": "",
            "name": str(name) if name is not None and not hasattr(name, "perform") else "",
            "namespace_stack": [],
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "executable",
            "plugins": [],
            "target": None,
            "cmd": "",
            "shell": self._shell,
        })


# ─── LaunchContext stub ────────────────────────────────────────────────────────

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
    filesystem paths via the ``_package_shares`` map.  If the target package
    exists in the lockfile but hasn't been fully fetched yet (no
    ``package.xml``), ``_PackageNotFetchedError`` is raised immediately so the
    Rust orchestrator can fetch the package and retry.

    In **non-preview mode** (post-build), all packages are installed and
    ``FindPackageShare`` returns real AMENT paths, so ``open()`` and
    ``os.path.*`` work natively.  No shimming is performed.
    """
    # Non-preview: packages are installed, paths are real — no shimming needed.
    if not _preview_mode:
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
        portable path.  Raises ``_PackageNotFetchedError`` when the package is
        in the lockfile but not fully fetched (missing ``package.xml``).
        """
        parsed = _parse_portable_path(path_str)
        if parsed is None:
            return None
        pkg, rest = parsed
        if pkg in _package_shares:
            pkg_dir = _package_shares[pkg]
            if not _orig_path_isfile(os.path.join(pkg_dir, "package.xml")):
                _packages_to_fetch.add(pkg)
                raise _PackageNotFetchedError(pkg)
            return os.path.join(pkg_dir, rest) if rest else pkg_dir
        # Try AMENT_PREFIX_PATH for packages not in the lockfile.
        if _real_get_package_share_directory is not None:
            try:
                share = _real_get_package_share_directory(pkg)
                return os.path.join(share, rest) if rest else share
            except _PackageNotFetchedError:
                raise
            except Exception:
                pass
        # Package not found anywhere that we know about — can't resolve.
        return None

    def _stub_open(path, mode="r", *args, **kwargs):
        path_str = str(path)
        # Handle portable paths.
        if _parse_portable_path(path_str) is not None:
            if not _apply_opaque_file_access:
                _error(
                    f"OpaqueFunction opened a portable path without --apply-opaque-file-access "
                    f"(stub returned): {path}"
                )
                return _io.StringIO(_STUB_ROS_PARAM_YAML)
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                try:
                    return _orig_open(actual, mode, *args, **kwargs)
                except (FileNotFoundError, OSError):
                    _error(f"param file not found: '{actual}' (resolved from '{path}') — stub defaults used")
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            _error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)
        # Regular (non-portable) path.
        try:
            return _orig_open(path, mode, *args, **kwargs)
        except (FileNotFoundError, OSError):
            # If the missing file is inside a lockfile package that hasn't been fully
            # fetched yet (no package.xml), raise _PackageNotFetchedError so Rust can
            # fetch the package and retry rather than silently returning stub YAML.
            for pkg_name, pkg_dir in _package_shares.items():
                pkg_dir_norm = pkg_dir.rstrip("/")
                if path_str.startswith(pkg_dir_norm + "/") or path_str.startswith(pkg_dir_norm + os.sep):
                    if not _orig_path_isfile(os.path.join(pkg_dir_norm, "package.xml")):
                        _packages_to_fetch.add(pkg_name)
                        raise _PackageNotFetchedError(pkg_name)
                    break  # package is fully fetched; file genuinely missing → error
            _error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)

    def _stub_path_exists(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.exists on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                return _orig_path_exists(actual)
            return False
        return _orig_path_exists(path)

    def _stub_path_isfile(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.isfile on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                return _orig_path_isfile(actual)
            return False
        return _orig_path_isfile(path)

    def _stub_path_isdir(path):
        path_str = str(path)
        if _parse_portable_path(path_str) is not None:
            if not _apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called os.path.isdir on a portable path without "
                    f"--apply-opaque-file-access (returning False): {path}"
                )
                return False
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                return _orig_path_isdir(actual)
            return False
        return _orig_path_isdir(path)

    def _stub_pathlib_open(path_self, mode='r', buffering=-1, encoding=None,
                           errors=None, newline=None):
        """Intercept Path.open() so that Path(...).read_text() also handles portable paths.

        pathlib.Path.read_text() calls self.open() internally, bypassing builtins.open.
        This stub redirects the call to the resolved actual path when the Path object
        holds a portable $(find-pkg-share ...) string.
        """
        path_str = str(path_self)
        if _parse_portable_path(path_str) is not None:
            if not _apply_opaque_file_access:
                _error(
                    f"OpaqueFunction called Path.open on a portable path without "
                    f"--apply-opaque-file-access (stub returned): {path_str}"
                )
                return _io.StringIO(_STUB_ROS_PARAM_YAML)
            actual = _resolve_portable(path_str)  # may raise _PackageNotFetchedError
            if actual is not None:
                try:
                    return _orig_pathlib_open(
                        _pathlib.Path(actual), mode, buffering, encoding, errors, newline
                    )
                except (FileNotFoundError, OSError):
                    _error(f"param file not found: '{actual}' (resolved from '{path_str}') — stub defaults used")
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            _error(f"param file not found: '{path_str}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)
        return _orig_pathlib_open(path_self, mode, buffering, encoding, errors, newline)

    def _patched_safe_load(stream):
        result = _orig_safe_load(stream)
        # Wrap ros__parameters dicts in _DefaultParamDict so that missing
        # keys return False rather than raising KeyError.
        if isinstance(result, dict):
            for ns_key, ns_val in result.items():
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
    return dict(_env)


def _resolve_node_details(node, context):
    """Fill in deferred details (package, executable, name, namespace, params, remaps, env).

    Works for both ``_TrackedNode`` / ``_TrackedLifecycleNode`` and
    ``_TrackedComposableNodeContainer`` — both expose the same raw fields.
    """
    entry = _tracked["nodes"][node._idx]

    # Package / executable / name: resolve substitutions (e.g. LaunchConfiguration)
    # that could not be resolved at construction time.
    for field in ("package", "executable", "name"):
        raw = getattr(node, f"_raw_{field}", None)
        if _is_substitution(raw):
            resolved, is_fallback = _resolve_substitution_ex(raw, context)
            if resolved is not None:
                entry[field] = resolved
                if field == "package" and not is_fallback:
                    _track_package(resolved)

    # Namespace: emit raw inputs — Rust computes effective_namespace from these
    ns = _resolve_substitution(node._raw_namespace, context) if node._raw_namespace is not None else None
    entry["namespace_stack"] = list(_namespace_stack)
    entry["explicit_namespace"] = ns

    # Parameters and param files
    params = {}
    pf_list = []
    for p in node._raw_parameters:
        if hasattr(p, "_param_file") and p._param_file:
            if p._param_file not in pf_list:
                pf_list.append(p._param_file)
        elif isinstance(p, dict):
            for k, v in p.items():
                resolved_v = _resolve_substitution(v, context)
                params[str(k)] = resolved_v if resolved_v is not None else ""
    entry["parameters"] = params
    entry["param_files"] = pf_list

    # Remappings: list of [src, dst] pairs
    remaps = []
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
            env[_resolve_substitution(k, context) or str(k)] = _resolve_substitution(v, context) or ""
    elif isinstance(raw_env, (list, tuple)):
        for item in raw_env:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                k_str = _resolve_substitution(item[0], context) or str(item[0])
                v_str = _resolve_substitution(item[1], context) or ""
                env[k_str] = v_str
    entry["env"] = env


def _resolve_composable_plugins(descs, context):
    """Convert ``_TrackedComposableNode`` descriptions to serialisable plugin dicts."""
    plugins = []
    for desc in descs:
        if not isinstance(desc, _TrackedComposableNode):
            # Fallback for real/unknown description objects
            pkg = getattr(desc, "package", None) or getattr(desc, "_package", None)
            plugin = getattr(desc, "plugin", None) or getattr(desc, "_plugin", None)
            if pkg or plugin:
                plugins.append({
                    "package": str(pkg) if pkg else "",
                    "plugin": str(plugin) if plugin else "",
                    "name": None,
                    "parameters": {},
                    "remappings": [],
                })
            continue
        params = {}
        for p in desc._raw_parameters:
            if isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = _resolve_substitution(v, context)
                    params[str(k)] = resolved_v if resolved_v is not None else ""
        remaps = []
        for r in desc._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = _resolve_substitution(r[0], context)
                dst = _resolve_substitution(r[1], context)
                remaps.append([
                    src if src is not None else str(r[0]),
                    dst if dst is not None else str(r[1]),
                ])
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
        plugins.append({
            "package": pkg,
            "plugin": plg,
            "name": nm or None,
            "parameters": params,
            "remappings": remaps,
        })
    return plugins


# ─── Inline Python include resolution ─────────────────────────────────────────

def _inline_resolve_python_launch(launch_file, parent_context, child_args, depth):
    """Load a Python launch file and walk its actions in the parent context.

    This mirrors real ROS 2 behavior where ``IncludeLaunchDescription``
    synchronously executes the child, so ``SetLaunchConfiguration`` calls in
    the child mutate the shared ``LaunchContext``.  The orchestrator still
    handles the recursive node/include dependency resolution separately.
    """
    global _declared_arg_names
    if depth > 20:
        _warn(f"Max inline include depth for {launch_file}")
        return

    # Resolve portable paths — $(find-pkg-share pkg)/rest → real filesystem path.
    real_path = launch_file
    parsed = _parse_portable_path(launch_file)
    if parsed:
        pkg, rest = parsed
        try:
            pkg_share = _resolve_pkg_share(pkg)
        except _PackageNotFetchedError:
            raise
        except Exception:
            return  # Package not available — orchestrator will resolve later
        real_path = os.path.join(pkg_share, rest)

    if not os.path.isfile(real_path):
        return  # File not on disk — orchestrator will fetch and resolve later

    try:
        spec = importlib.util.spec_from_file_location(
            f"_inline_launch_{depth}", real_path
        )
        if spec is None or spec.loader is None:
            _warn(f"cannot load included launch file: {real_path}")
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except _PackageNotFetchedError:
        raise
    except Exception as e:
        _warn(f"failed to load included launch file {real_path}: {e}")
        return

    if not hasattr(mod, "generate_launch_description"):
        return

    # Save tracking state BEFORE generate_launch_description() — constructors
    # (e.g. _TrackedNode, _TrackedSetParameter) append to _tracked["nodes"]
    # at construction time.  The Rust orchestrator will resolve this same
    # child file separately and produce its own tracked entries, so we must
    # discard everything created by the inline execution.
    saved_nodes_len = len(_tracked["nodes"])
    saved_gp_len = len(_tracked["global_params"])
    saved_deps_len = len(_tracked["include_deps"])
    saved_pkgs = list(_tracked["packages"])
    saved_includes_len = len(_tracked["includes"])
    saved_include_args_keys = set(_tracked["include_args"])
    saved_param_files_len = len(_tracked["param_files"])
    saved_param_file_deps_len = len(_tracked["param_file_deps"])
    saved_declared_args_len = len(_tracked["declared_args"])
    saved_declared_arg_names = set(_declared_arg_names)
    saved_event_handlers_len = len(_tracked["event_handlers"])
    saved_namespace_depth = len(_namespace_stack)
    saved_env = dict(_env)

    # Snapshot parent context BEFORE generate_launch_description() so we can
    # fully restore it after the inline walk.  Only deliberate side-effects
    # (SetLaunchConfiguration) survive.
    saved_configs = dict(parent_context._launch_configurations)

    # Most _tracked mutations (from constructors in generate_launch_description
    # AND from _walk_actions) must be rolled back on any exit path, including
    # exceptions from generate_launch_description() itself.
    #
    # Intentionally preserved side-effects (NOT rolled back):
    #   - set_launch_configurations: the whole purpose of inline includes
    #   - warnings / errors: diagnostic messages should propagate to the user
    # Everything else in _tracked is rolled back in the finally block below.
    try:
        try:
            ld = mod.generate_launch_description()
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"generate_launch_description() failed in {launch_file}: {e}")
            return

        entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

        # Apply child launch arguments: only set keys that the parent hasn't
        # already defined, so parent values are never overwritten.
        for k, v in child_args.items():
            if k not in parent_context._launch_configurations:
                parent_context._launch_configurations[k] = v

        # Pass 1: apply DeclareLaunchArgument defaults (child-only args).
        for entity in entities:
            if isinstance(entity, _DeclaredArg):
                _apply_declared_arg(entity, parent_context)

        # Pass 2: walk actions — SetLaunchConfiguration, OpaqueFunction, etc.
        # all mutate parent_context directly, which is the desired effect.
        # Only context mutations survive; tracked state is rolled back.
        _walk_actions(entities, parent_context, depth)
    except _PackageNotFetchedError:
        raise
    finally:
        del _tracked["nodes"][saved_nodes_len:]
        del _tracked["global_params"][saved_gp_len:]
        del _tracked["include_deps"][saved_deps_len:]
        del _tracked["includes"][saved_includes_len:]
        for k in list(_tracked["include_args"]):
            if k not in saved_include_args_keys:
                del _tracked["include_args"][k]
        del _tracked["param_files"][saved_param_files_len:]
        del _tracked["param_file_deps"][saved_param_file_deps_len:]
        _tracked["packages"][:] = saved_pkgs
        del _tracked["declared_args"][saved_declared_args_len:]
        _declared_arg_names.clear()
        _declared_arg_names.update(saved_declared_arg_names)
        del _tracked["event_handlers"][saved_event_handlers_len:]
        del _namespace_stack[saved_namespace_depth:]
        _env.clear()
        _env.update(saved_env)

    # Restore child-only args that were not SetLaunchConfiguration'd —
    # child DeclareLaunchArgument defaults should NOT leak into the parent
    # scope, only SetLaunchConfiguration is a deliberate side-effect.
    # Keep keys that were either already in the parent or were set via
    # SetLaunchConfiguration.  Also preserve "global_params" — this is the
    # accumulation list for SetParameter, not a launch argument.
    set_configs = set(_tracked["set_launch_configurations"].keys())
    for k in list(parent_context._launch_configurations):
        if k not in saved_configs and k not in set_configs and k != "global_params":
            del parent_context._launch_configurations[k]


# ─── Action walker ────────────────────────────────────────────────────────────

def _walk_actions(actions, context, depth=0):
    if depth > 20:
        _warn("Max include depth reached while walking Python launch description")
        return
    if actions is None:
        return
    for action in actions:
        if action is None:
            continue
        try:
            _walk_action(action, context, depth)
        except _PackageNotFetchedError:
            raise  # Let it propagate to the OpaqueFunction or top-level handler
        except Exception as e:
            _error(f"Error walking action {type(action).__name__}: {e}")

def _walk_action(action, context, depth):
    cls_name = type(action).__name__

    # DeclareLaunchArgument: apply default to context so subsequent actions can use it.
    # This handles nested declarations (inside GroupAction etc.); top-level ones are
    # already processed in the two-pass logic in main().
    if isinstance(action, _DeclaredArg):
        _apply_declared_arg(action, context)
        return

    # Our tracked nodes — resolve details deferred with the live context
    if isinstance(action, (_TrackedNode, _TrackedLifecycleNode)):
        if context is not None and not action._detailed:
            action._detailed = True
            _resolve_node_details(action, context)
        return

    if isinstance(action, _TrackedComposableNodeContainer):
        if context is not None and not action._detailed:
            action._detailed = True
            _resolve_node_details(action, context)
            _tracked["nodes"][action._idx]["plugins"] = _resolve_composable_plugins(
                action._descs, context
            )
        return

    if isinstance(action, _TrackedLoadComposableNodes):
        if context is not None and not action._detailed:
            action._detailed = True
            entry = _tracked["nodes"][action._idx]
            if action._raw_target is not None:
                if isinstance(action._raw_target, _TrackedComposableNodeContainer):
                    # Container object: use its already-resolved name (updated by _resolve_node_details).
                    target = _tracked["nodes"][action._raw_target._idx].get("name") or entry.get("target", "")
                else:
                    target = _resolve_substitution(action._raw_target, context)
                if target:
                    entry["target"] = target
            entry["plugins"] = _resolve_composable_plugins(action._descs, context)
        return

    if isinstance(action, _TrackedPushRosNamespace):
        if action._namespace is not None and context is not None:
            ns = _resolve_substitution(action._namespace, context)
            if ns:
                _namespace_stack.append(ns)
        return

    if isinstance(action, _TrackedIncludeLaunchDescription):
        # Deferred resolution: when the source is a substitution (e.g. PathJoinSubstitution),
        # the path cannot be extracted at construction time because no LaunchContext is
        # available.  Try again here with the live context provided by _walk_action.
        if action._path is None and context is not None:
            src = action._source
            path = None
            if hasattr(src, "perform"):
                try:
                    path = src.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception as e:
                    _warn(f"failed to resolve IncludeLaunchDescription source: {e}")
            if path:
                action._path = path
                dep_idx = _track_include(path)
                _resolve_include_args(path, action._raw_launch_arguments, context, dep_idx)

        # Inline-execute Python includes within the parent context so that
        # SetLaunchConfiguration side-effects propagate to sibling actions,
        # just like the real ROS 2 launch system processes includes synchronously.
        if action._path and action._path.endswith(".py") and context is not None:
            child_args = {}
            if action._raw_launch_arguments:
                for k, v in action._raw_launch_arguments:
                    k_str = str(k)
                    resolved = _resolve_substitution(v, context)
                    v_str = resolved if resolved is not None else str(v)
                    child_args[k_str] = v_str
            _inline_resolve_python_launch(action._path, context, child_args, depth + 1)

        return

    # SetParameter: append (name, value) to context['global_params'], mirroring the real
    # launch_ros SetParameter.execute() which does:
    #   global_param_list = context.launch_configurations.get('global_params', [])
    #   global_param_list.extend(eval_param_dict.items())
    #   context.launch_configurations['global_params'] = global_param_list
    if isinstance(action, _TrackedSetParameter):
        name = action._name
        value = action._value
        # Resolve name substitution if needed
        if hasattr(name, "perform") and context is not None:
            try:
                name = name.perform(context)
            except _PackageNotFetchedError:
                raise
            except Exception:
                name = str(name)
        else:
            name = str(name) if name is not None else ""
        if name and context is not None:
            # Resolve value substitution if needed
            if hasattr(value, "perform"):
                try:
                    value = value.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    pass
            # Coerce string representations of numerics to native Python types so that
            # arithmetic in consumer files (e.g. gp["front_overhang"] + gp["wheel_base"])
            # works without explicit casts.
            if isinstance(value, str):
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        pass
            gp_list = context._launch_configurations.setdefault("global_params", [])
            gp_list.append((name, value))
            _tracked["global_params"].append([name, value])
            # Update the tracked node entry so the orchestrator can render <set_parameter>.
            _tracked["nodes"][action._idx]["name"] = name
            _tracked["nodes"][action._idx]["param_value"] = str(value) if value is not None else ""
        return

    # ExecuteProcess: resolve cmd parts and name, record as executable.
    if isinstance(action, _TrackedExecutable):
        parts = []
        for part in action._cmd:
            raw_part = part
            if hasattr(part, "perform") and context is not None:
                try:
                    result = part.perform(context)
                    part = result if result is not None else raw_part
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    part = str(raw_part)
            parts.append(str(part))
        cmd_str = " ".join(parts)
        name = action._name
        if hasattr(name, "perform") and context is not None:
            try:
                name = name.perform(context)
            except _PackageNotFetchedError:
                raise
            except Exception:
                name = str(name) if name is not None else ""
        name_str = str(name) if name is not None else ""
        _tracked["nodes"][action._idx]["cmd"] = cmd_str
        _tracked["nodes"][action._idx]["name"] = name_str
        _tracked["nodes"][action._idx]["shell"] = action._shell
        return

    # SetLaunchConfiguration: update launch_configurations at walk time.
    if isinstance(action, _SetLaunchConfiguration):
        name = action._name
        value = action._value
        if name and context is not None:
            if hasattr(value, "perform"):
                try:
                    value = value.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    pass
            resolved_value = str(value) if value is not None else ""
            context._launch_configurations[str(name)] = resolved_value
            _tracked["set_launch_configurations"][str(name)] = resolved_value
        return

    # OpaqueFunction: execute its function and walk the result
    if isinstance(action, _TrackedOpaqueFunction) or (
        cls_name == "OpaqueFunction" or
        (hasattr(action, "function") and callable(getattr(action, "function", None)))
    ):
        fn = getattr(action, "function", None)
        if fn and context:
            try:
                result = _call_opaque_with_stubs(fn, context)
                if result:
                    _walk_actions(result, context, depth + 1)
            except _PackageNotFetchedError:
                pass  # Package recorded in _packages_to_fetch; Rust will fetch and retry
            except Exception as e:
                _error(f"OpaqueFunction failed: {e}")
        return

    # SetEnvironmentVariable: mutate the env map (respects condition)
    if isinstance(action, _TrackedSetEnvironmentVariable):
        if action._condition is not None and context is not None:
            try:
                if not action._condition.evaluate(context):
                    return
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"SetEnvironmentVariable condition evaluation failed: {e}")
                return
        name = _to_str(action._name, context)
        if not name:
            _error("SetEnvironmentVariable: resolved name is empty or None — skipping")
            return
        value = _to_str(action._value, context) or ""
        _env[name] = value
        return

    # UnsetEnvironmentVariable: remove from env map (respects condition)
    if isinstance(action, _TrackedUnsetEnvironmentVariable):
        if action._condition is not None and context is not None:
            try:
                if not action._condition.evaluate(context):
                    return
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"UnsetEnvironmentVariable condition evaluation failed: {e}")
                return
        name = _to_str(action._name, context)
        if not name:
            _error("UnsetEnvironmentVariable: resolved name is empty or None — skipping")
            return
        if name in os.environ:
            # In process env (cases 2 & 3) — can't unset baseline.
            _error(
                f"unset_env: '{name}' exists in the process env and cannot be unset. "
                f"Use SetEnvironmentVariable(name=\"{name}\", value=\"\") "
                "or a scoped group instead"
            )
        elif name in _env:
            # Case 1: override-only, no baseline to expose — safe to remove.
            del _env[name]
        else:
            # Case 4: not set anywhere.
            _error(f"unset_env: environment variable '{name}' is not set")
        return

    # GroupAction: walk child actions; scoped groups save/restore env + namespace
    if isinstance(action, _TrackedGroupAction):
        if action._condition is not None and context is not None:
            try:
                if not action._condition.evaluate(context):
                    return
            except _PackageNotFetchedError:
                raise
            except Exception as e:
                _warn(f"GroupAction condition evaluation failed: {e}")
                return
        depth_before = len(_namespace_stack)
        saved_env = dict(_env) if action._scoped else None
        _walk_actions(action._actions, context, depth + 1)
        del _namespace_stack[depth_before:]
        if saved_env is not None:
            _env.clear()
            _env.update(saved_env)
        return

    if isinstance(action, _TimerAction):
        _walk_actions(action._actions, context, depth + 1)
        return

    if hasattr(action, "entities"):
        try:
            _walk_actions(action.entities, context, depth + 1)
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"failed to walk {cls_name}.entities: {e}")
    if hasattr(action, "_actions"):
        try:
            _walk_actions(action._actions, context, depth + 1)
        except _PackageNotFetchedError:
            raise
        except Exception as e:
            _warn(f"failed to walk {cls_name}._actions: {e}")

    # Real Node/LifecycleNode from launch_ros (unpatched, e.g. from OpaqueFunction return)
    if cls_name in ("Node", "LifecycleNode") and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        _track_node(pkg, exe)
    elif cls_name == "ComposableNodeContainer" and hasattr(action, "_package"):
        pkg = getattr(action, "_package", None)
        exe = getattr(action, "_node_executable", getattr(action, "_node_name", None))
        name = getattr(action, "_name", None)
        _track_node(pkg, exe, name)
        descs = getattr(action, "composable_node_descriptions",
                        getattr(action, "_composable_node_descriptions", None)) or []
        for desc in descs:
            _track_node(getattr(desc, "package", None),
                        getattr(desc, "plugin", None),
                        getattr(desc, "node_name", getattr(desc, "name", None)))
    elif cls_name == "LoadComposableNodes":
        descs = getattr(action, "_composable_node_descriptions",
                        getattr(action, "composable_node_descriptions", None)) or []
        for desc in descs:
            _track_node(getattr(desc, "package", None),
                        getattr(desc, "plugin", None),
                        getattr(desc, "node_name", getattr(desc, "name", None)))

    # IncludeLaunchDescription (real)
    if cls_name == "IncludeLaunchDescription":
        src = getattr(action, "_launch_description_source", None)
        if src:
            loc = getattr(src, "location", None)
            if callable(loc) and context:
                try:
                    loc = loc(context)
                except _PackageNotFetchedError:
                    raise  # Let it propagate to the OpaqueFunction or top-level handler
                except Exception:
                    loc = None
            if loc:
                _track_include(loc)

    # Warn about action classes we do not recognise.  Any class not in the known set may
    # represent a node, include, or container that will be absent from the resolved output.
    if cls_name not in _KNOWN_ACTION_CLASSES:
        _warn(
            f"Unrecognised action type '{cls_name}' — any nodes or includes it "
            f"declares may not appear in the resolved output"
        )

# ─── Known action class names ─────────────────────────────────────────────────

# Class names that _walk_action handles explicitly or that are safe to skip.
# Any class not in this set triggers a warning so the user knows the output may
# be incomplete.  The set covers:
#   • Our shimmed tracking classes (prefixed with underscore or "Tracked")
#   • Real launch_ros classes handled via cls_name duck-typing
#   • ROS 2 lifecycle / event infrastructure that produces no nodes/includes
_KNOWN_ACTION_CLASSES: frozenset = frozenset({
    # Shimmed tracking classes
    "_DeclaredArg",
    "_TrackedNode",
    "_TrackedLifecycleNode",
    "_TrackedComposableNodeContainer",
    "_TrackedLoadComposableNodes",
    "_TrackedPushRosNamespace",
    "_TrackedIncludeLaunchDescription",
    "_TrackedSetParameter",
    "_TrackedExecutable",
    "_SetLaunchConfiguration",
    "_TrackedOpaqueFunction",
    "_TrackedGroupAction",
    "_TrackedRegisterEventHandler",
    "_TrackedEmitEvent",
    "_TrackedOnProcessStart",
    "_TrackedOnProcessExit",
    "_TrackedOnStateTransition",
    "_TrackedOnShutdown",
    "_TimerAction",
    # Real launch_ros classes handled via cls_name fallback in _walk_action
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
})

# ─── Import system patcher ────────────────────────────────────────────────────

_PATCHED_MODULES = {}

def _build_patched_launch():
    """Stub for the top-level `launch` package (used when ROS 2 is not installed)."""
    mod = types.ModuleType("launch")
    mod.__path__ = []      # mark as package so submodule imports work
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
    mod.FindPackageShare = _TrackedFindPackageShare
    mod.PathJoinSubstitution = _TrackedPathJoinSubstitution
    mod.LaunchConfiguration = _LaunchConfiguration
    _SENTINEL = object()
    class _DeferredEnvironmentVariable:
        """Deferred substitution: reads _env at perform() time, not construction."""
        def __init__(self, name, **kw):
            self._name = name
            self._default = kw.get("default_value", _SENTINEL)
        def perform(self, context=None):
            # Resolve name to a concrete string via _to_str.
            name = _to_str(self._name, context) or ""
            # Look up in override env, then process env.
            if name in _env:
                return _env[name]
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
                if hasattr(self._condition, 'perform'):
                    val = str(self._condition.perform(context)).lower()
                    return val in ('true', '1', 'yes', 'on')
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
        # yet (no package.xml), _PackageNotFetchedError is raised here — which is important
        # for module-level callers where os.path stubs may not be active.
        if package_name in _package_shares:
            pkg_dir = _package_shares[package_name]
            if not os.path.isfile(os.path.join(pkg_dir, "package.xml")):
                _packages_to_fetch.add(package_name)
                raise _PackageNotFetchedError(package_name)
        # In non-preview mode, return the real install path so the output contains
        # absolute paths matching the installed layout.
        if not _preview_mode and package_name in _package_shares:
            return _package_shares[package_name]
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

# ─── Main ─────────────────────────────────────────────────────────────────────

def _emit(obj):
    """Write JSON to the real stdout (saved before redirect)."""
    _emit._fd.write(json.dumps(obj))
    _emit._fd.write("\n")
    _emit._fd.flush()


def main():
    # Redirect stdout → stderr so that print() calls inside user launch files
    # (e.g. OpaqueFunction bodies) don't contaminate our JSON output.
    _emit._fd = sys.stdout
    sys.stdout = sys.stderr

    if len(sys.argv) < 3:
        _emit({"error": "usage: py_resolver.py <launch_file> <args_json> [package_shares_json]"})
        sys.exit(1)

    launch_file = sys.argv[1]
    args_dict = json.loads(sys.argv[2])

    # Load workspace package share paths (passed by orchestrator as third argument).
    # These let us resolve $(find-pkg-share X) against workspace source packages
    # before falling back to AMENT_PREFIX_PATH or hardcoded paths.
    global _package_shares, _namespace_stack, _apply_opaque_file_access
    _namespace_stack = []
    _env.clear()
    if len(sys.argv) > 3:
        try:
            _package_shares = json.loads(sys.argv[3])
        except Exception:
            _package_shares = {}

    # Load workflow flags (passed as 5th argument, JSON object).
    global _preview_mode
    if len(sys.argv) > 4:
        try:
            flags = json.loads(sys.argv[4])
            _apply_opaque_file_access = bool(flags.get("apply_opaque_file_access", False))
            _preview_mode = bool(flags.get("preview", True))
            _lockfile_packages = set(flags.get("lockfile_packages", []))
        except Exception:
            _apply_opaque_file_access = False

    # Arg values may contain portable paths ($(find-pkg-share pkg)/...) when they are
    # cascaded from the XML resolver.  These are intentionally preserved as-is: the
    # _stub_open and os.path stubs inside _call_opaque_with_stubs handle them lazily,
    # resolving only when the OpaqueFunction actually accesses the filesystem.

    # Install the import patcher before anything imports launch.
    # Also force our stubs into sys.modules to displace any already-imported real modules.
    # Python checks sys.modules before consulting meta path finders, so the real
    # ament_index_python.packages (imported above for _real_get_package_share_directory)
    # would otherwise bypass our stub entirely.
    sys.meta_path.insert(0, _PatchingFinder())
    for mod_name, builder in _PatchingFinder.PATCHED.items():
        if mod_name not in _PATCHED_MODULES:
            _PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = _PATCHED_MODULES[mod_name]

    # Load the launch file as a module
    spec = importlib.util.spec_from_file_location("_target_launch", launch_file)
    if spec is None:
        _emit({"error": f"cannot load {launch_file}"})
        sys.exit(1)

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except _PackageNotFetchedError:
        # Module-level get_package_share_directory() call failed: source not on disk.
        # Output with packages_to_fetch set so Rust fetches and retries.
        _tracked["packages_to_fetch"] = sorted(_packages_to_fetch)
        _emit(_tracked)
        return
    except Exception as e:
        _error(f"Error loading launch file: {e}")
        _emit(_tracked)
        return

    # Call generate_launch_description()
    if not hasattr(mod, "generate_launch_description"):
        _error("No generate_launch_description() function found")
        _emit(_tracked)
        return

    # Pre-populate global_params from the orchestrator's persisted context so that
    # files that read context.launch_configurations["global_params"] (e.g. to access
    # vehicle dimensions set by SetParameter in a sibling file) find the data already
    # there.  The key is stripped from args_dict before building the launch context.
    persisted_global_params = []
    if "__global_params__" in args_dict:
        try:
            persisted_global_params = json.loads(args_dict.pop("__global_params__"))
        except Exception:
            pass

    ctx = _make_launch_context(args_dict)

    if persisted_global_params:
        # Store as list of (name, value) tuples matching real ROS 2 SetParameter layout.
        ctx._launch_configurations["global_params"] = [
            (entry[0], entry[1]) for entry in persisted_global_params if len(entry) == 2
        ]

    try:
        ld = mod.generate_launch_description()
    except _PackageNotFetchedError:
        _tracked["packages_to_fetch"] = sorted(_packages_to_fetch)
        _emit(_tracked)
        return
    except Exception as e:
        _error(f"generate_launch_description() failed: {e}")
        _emit(_tracked)
        return

    # Walk the LaunchDescription tree — two passes, mirroring the XML resolver's behavior:
    #
    # Pass 1 (sequential): Apply all top-level DeclareLaunchArgument defaults to the context
    #   in list order.  This populates ctx._launch_configurations so that a later declaration
    #   can reference an earlier one via LaunchConfiguration("prev_arg"), exactly as the XML
    #   resolver processes <arg default="..."> elements sequentially into ctx.args.
    #
    # Pass 2: Walk all actions.  OpaqueFunction bodies now have a fully populated context.
    entities = getattr(ld, "entities", None) or getattr(ld, "_actions", None) or []

    for entity in entities:
        if isinstance(entity, _DeclaredArg):
            _apply_declared_arg(entity, ctx)

    try:
        _walk_actions(entities, ctx)
    except _PackageNotFetchedError:
        pass  # Absorbed; package already in _packages_to_fetch

    # Net-zero check: any remaining overrides are leaked env mutations.
    for k, v in _env.items():
        _error(f"env var '{k}' was set to '{v}' but not restored (leaked from file scope)")

    if _packages_to_fetch:
        _tracked["packages_to_fetch"] = sorted(_packages_to_fetch)
    _emit(_tracked)

if __name__ == "__main__":
    main()
