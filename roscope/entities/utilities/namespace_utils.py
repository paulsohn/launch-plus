"""Namespace utilities matching official launch_ros/utilities/namespace_utils.py."""

from __future__ import annotations


def make_namespace_absolute(namespace: str | None) -> str | None:
    """Make a relative namespace absolute."""
    if namespace is None:
        return None
    if not namespace.startswith("/"):
        return "/" + namespace
    return namespace


def prefix_namespace(base_ns: str | None, ns: str | None) -> str | None:
    """Return ``ns`` prefixed with ``base_ns`` if ``ns`` is relative.

    - Both None → None
    - ``ns`` is None → ``base_ns``
    - ``base_ns`` is None or empty → ``ns``
    - ``ns`` is absolute → ``ns``
    - Otherwise → ``base_ns/ns`` (trailing ``/`` stripped)
    """
    if base_ns is None and ns is None:
        return None
    if ns is None:
        combined = base_ns
    elif not base_ns or (ns and ns.startswith("/")):
        combined = ns
    else:
        if base_ns == "/":
            base_ns = ""
        combined = base_ns + "/" + ns
    if combined and combined != "/" and combined.endswith("/"):
        combined = combined.rstrip("/")
    return combined
