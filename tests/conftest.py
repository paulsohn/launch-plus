"""Shared fixtures for resolver tests."""

import sys

import pytest

from launch_plus.resolver import _PATCHED_MODULES, _PatchingFinder


def _install_import_patching():
    """Install the import patcher so child launch files can import shim modules."""
    if not any(isinstance(f, _PatchingFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _PatchingFinder())
    for mod_name, builder in _PatchingFinder.PATCHED.items():
        if mod_name not in _PATCHED_MODULES:
            _PATCHED_MODULES[mod_name] = builder()
        sys.modules[mod_name] = _PATCHED_MODULES[mod_name]


_install_import_patching()


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    """No global state to reset — each test creates its own context."""
    yield
