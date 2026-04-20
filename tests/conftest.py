"""Shared fixtures for resolver tests."""

import sys

import pytest

from roscope.resolver import _PATCHED_MODULES, _PatchingFinder


def _install_import_patching():
    """Install the import patcher so child launch files can import shim modules."""
    if not any(isinstance(f, _PatchingFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _PatchingFinder())
    for mod_name, builder in _PatchingFinder.PATCHED.items():
        if mod_name not in _PATCHED_MODULES:
            _PATCHED_MODULES[mod_name] = builder()
        mod = _PATCHED_MODULES[mod_name]
        sys.modules[mod_name] = mod
        if "." in mod_name:
            parent_name, _, child_name = mod_name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child_name, mod)


_install_import_patching()


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    """No global state to reset — each test creates its own context."""
    yield
