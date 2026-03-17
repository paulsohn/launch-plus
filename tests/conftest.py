"""Shared fixtures for py_resolver tests."""

import copy
import sys
import pytest

from launch_plus import py_resolver as R


# Snapshot the initial state of all py_resolver globals so each test starts clean.
_TRACKED_TEMPLATE = copy.deepcopy(R._tracked)


def _install_import_patching():
    """Install the import patcher so child launch files can import shim modules."""
    # Add the patching finder if not already present.
    if not any(isinstance(f, R._PatchingFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, R._PatchingFinder())
    # Force patched modules into sys.modules.
    for mod_name, builder in R._PatchingFinder.PATCHED.items():
        if mod_name not in R._PATCHED_MODULES:
            R._PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = R._PATCHED_MODULES[mod_name]


# Install once at import time so all tests can load child launch files.
_install_import_patching()


@pytest.fixture(autouse=True)
def _reset_py_resolver_state():
    """Reset py_resolver module-level globals before every test."""
    # Restore _tracked to a fresh deep-copy of the template.
    R._tracked.clear()
    R._tracked.update(copy.deepcopy(_TRACKED_TEMPLATE))

    # Scalar / set / list globals
    R._packages_to_fetch.clear()
    R._declared_arg_names.clear()
    R._namespace_stack.clear()
    R._package_shares.clear()
    R._lockfile_packages.clear()
    R._apply_opaque_file_access = False
    R._preview_mode = True
    R._env.clear()
    R._env_baseline.clear()

    yield

    # No teardown needed — next invocation resets again.
