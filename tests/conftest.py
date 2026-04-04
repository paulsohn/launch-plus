"""Shared fixtures for resolver tests."""

import sys

import pytest

from launch_plus import resolver as R


def _install_import_patching():
    """Install the import patcher so child launch files can import shim modules."""
    if not any(isinstance(f, R._PatchingFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, R._PatchingFinder())
    for mod_name, builder in R._PatchingFinder.PATCHED.items():
        if mod_name not in R._PATCHED_MODULES:
            R._PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = R._PATCHED_MODULES[mod_name]


_install_import_patching()


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    """No global state to reset — each test creates its own context."""
    yield
