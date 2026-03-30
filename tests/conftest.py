"""Shared fixtures for resolver tests."""

import sys

import pytest

from launch_plus import resolver as R


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
def _reset_resolver_state():
    """Reset resolver module-level state before every test."""
    R.get_state().reset()
    # Test defaults differ from production defaults:
    R.get_state().preview_mode = True
    R.get_state().apply_arg_defaults = True
    R.get_state().global_arg_cascade = True
    yield
    # No teardown needed — next invocation resets again.
