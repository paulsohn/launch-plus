"""Connection metadata plugin loader, validator, and conflict checker."""

from __future__ import annotations

import importlib.util
import logging

logger = logging.getLogger("roscope")

RECOGNIZED_TYPES: frozenset[str] = frozenset(
    {
        "publisher",
        "subscription",
        "service_client",
        "service_server",
        "action_client",
        "action_server",
    }
)

PROTOCOL_FAMILY: dict[str, str] = {
    "publisher": "topic",
    "subscription": "topic",
    "service_client": "service",
    "service_server": "service",
    "action_client": "action",
    "action_server": "action",
}


def load_plugin(path: str):
    """Load get_connections callable from a plugin file.

    Returns the callable on success, or None if loading fails.
    """
    try:
        spec = importlib.util.spec_from_file_location("_roscope_connection_plugin", path)
        if spec is None or spec.loader is None:
            logger.warning("--plugin: cannot load %s", path)
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "get_connections", None)
        if not callable(fn):
            logger.warning("--plugin: %s has no callable get_connections()", path)
            return None
        return fn
    except Exception as e:
        logger.warning("--plugin: failed to load %s: %s", path, e)
        return None


def call_plugin(
    plugin_fn,
    pkg_share_path: str,
    executable: str,
    params: dict,
) -> dict[str, dict]:
    """Invoke the plugin and return validated connection metadata.

    - Plugin raises → logged as an error; returns {} (unexpected execution failure).
    - Plugin returns non-dict (including None) → logged as a warning; returns {}
      (return {} to indicate no metadata).
    Entries whose 'type' is an unrecognized string are warned and dropped.
    """
    if plugin_fn is None:
        return {}
    try:
        result = plugin_fn(pkg_share_path, executable, params)
    except Exception as e:
        logger.error("--plugin: raised for %s/%s: %s", pkg_share_path, executable, e)
        return {}

    if not isinstance(result, dict):
        logger.warning(
            "--plugin: get_connections returned %s for %s/%s; expected dict",
            type(result).__name__,
            pkg_share_path,
            executable,
        )
        return {}

    validated: dict[str, dict] = {}
    for connection, meta in result.items():
        if not isinstance(meta, dict):
            logger.warning("--plugin: entry %r has non-dict metadata; skipping", connection)
            continue
        conn_type = meta.get("type")
        if conn_type is not None and conn_type not in RECOGNIZED_TYPES:
            logger.warning("--plugin: %r has unrecognized type %r; skipping", connection, conn_type)
            continue
        validated[connection] = meta

    return validated
