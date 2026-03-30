"""OpaqueFunction stubs for file access in preview mode.

Patches ``builtins.open``, ``yaml.safe_load``, and ``os.path`` predicates
during OpaqueFunction execution so that portable ``$(find-pkg-share pkg)/...``
paths are intercepted and resolved to actual filesystem paths.
"""

from __future__ import annotations

import builtins as _builtins
import io as _io
import os
import pathlib as _pathlib

import launch_plus.resolver as _R

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


def _call_opaque_with_stubs(state, fn, context):
    """Call ``fn(context)`` with file-not-found stubs active.

    In **preview mode**, patches ``builtins.open``, ``yaml.safe_load``, and
    ``os.path`` predicates for the duration of the call so that portable
    ``$(find-pkg-share pkg)/...`` paths are intercepted and resolved to actual
    filesystem paths via the ``state.package_shares`` map.  If the target package
    exists in the lockfile but hasn't been fully fetched yet (no
    ``package.xml``), ``_ensure_fetched()`` is called inline.  If fetching fails,
    an error is logged and a stub/fallback is returned.

    In **non-preview mode** (post-build), all packages are installed and
    ``FindPackageShare`` returns real AMENT paths, so ``open()`` and
    ``os.path.*`` work natively.  No shimming is performed.
    """
    # Non-preview: packages are installed, paths are real — no shimming needed.
    if not state.preview_mode:
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
        parsed = _R._parse_portable_path(path_str)
        if parsed is None:
            return None
        pkg, rest = parsed
        if pkg in state.package_shares:
            pkg_dir = state.package_shares[pkg]
            if not _orig_path_isfile(os.path.join(pkg_dir, "package.xml")):
                if _R._ensure_fetched(state, pkg):
                    pkg_dir = state.package_shares[pkg]
                else:
                    state.error(f"failed to fetch package '{pkg}' from lockfile")
                    return None
            return os.path.join(pkg_dir, rest) if rest else pkg_dir
        # Try fetching if it's a lockfile package.
        if pkg in state.lockfile_data and _R._ensure_fetched(state, pkg):
            pkg_dir = state.package_shares[pkg]
            return os.path.join(pkg_dir, rest) if rest else pkg_dir
        # Try AMENT_PREFIX_PATH for packages not in the lockfile.
        if _R._real_get_package_share_directory is not None:
            try:
                share = _R._real_get_package_share_directory(pkg)
                return os.path.join(share, rest) if rest else share
            except Exception:
                pass
        # Package not found anywhere that we know about — can't resolve.
        return None

    def _stub_open(path, mode="r", *args, **kwargs):
        path_str = str(path)
        # Handle portable paths.
        if _R._parse_portable_path(path_str) is not None:
            if not state.apply_opaque_file_access:
                state.error(
                    f"OpaqueFunction opened a portable path without --apply-opaque-file-access "
                    f"(stub returned): {path}"
                )
                return _io.StringIO(_STUB_ROS_PARAM_YAML)
            actual = _resolve_portable(path_str)
            if actual is not None:
                try:
                    return _orig_open(actual, mode, *args, **kwargs)
                except (FileNotFoundError, OSError):
                    state.error(
                        f"param file not found: '{actual}' "
                        f"(resolved from '{path}') — stub defaults used"
                    )
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            state.error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)
        # Regular (non-portable) path.
        try:
            return _orig_open(path, mode, *args, **kwargs)
        except (FileNotFoundError, OSError):
            # If the missing file is inside a lockfile package that hasn't been fully
            # fetched yet (no package.xml), try to fetch it inline.
            for pkg_name, pkg_dir in list(state.package_shares.items()):
                pkg_dir_norm = pkg_dir.rstrip("/")
                if path_str.startswith(pkg_dir_norm + "/") or path_str.startswith(
                    pkg_dir_norm + os.sep
                ):
                    if not _orig_path_isfile(os.path.join(pkg_dir_norm, "package.xml")):
                        if _R._ensure_fetched(state, pkg_name):
                            # Retry open after fetching.
                            try:
                                return _orig_open(path, mode, *args, **kwargs)
                            except (FileNotFoundError, OSError):
                                pass  # File still missing after fetch → fall through to error
                        else:
                            state.error(f"failed to fetch package '{pkg_name}' from lockfile")
                            break
                    break  # package is fully fetched; file genuinely missing → error
            state.error(f"param file not found: '{path}' — stub defaults used")
            return _io.StringIO(_STUB_ROS_PARAM_YAML)

    def _stub_path_exists(path):
        path_str = str(path)
        if _R._parse_portable_path(path_str) is not None:
            if not state.apply_opaque_file_access:
                state.error(
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
        if _R._parse_portable_path(path_str) is not None:
            if not state.apply_opaque_file_access:
                state.error(
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
        if _R._parse_portable_path(path_str) is not None:
            if not state.apply_opaque_file_access:
                state.error(
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
        if _R._parse_portable_path(path_str) is not None:
            if not state.apply_opaque_file_access:
                state.error(
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
                    state.error(
                        f"param file not found: '{actual}' "
                        f"(resolved from '{path_str}') — stub defaults used"
                    )
                    return _io.StringIO(_STUB_ROS_PARAM_YAML)
            state.error(f"param file not found: '{path_str}' — stub defaults used")
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
