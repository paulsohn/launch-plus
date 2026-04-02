"""Local disk cache for visualizer snapshots.

Layout:
    ~/.cache/launch-plus-viz/
        server.json              # Running server info: { "port": ..., "pid": ... }
        <viz-id>/
            <timestamp>.json     # Snapshot: { "metadata": ..., "nodes": ..., ... }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_env_cache = os.environ.get("LAUNCH_PLUS_VIZ_CACHE", "")
_CACHE_ROOT = Path(_env_cache) if _env_cache else (Path.home() / ".cache" / "launch-plus-viz")


def cache_root() -> Path:
    return _CACHE_ROOT


def server_json_path() -> Path:
    return _CACHE_ROOT / "server.json"


def save_snapshot(viz_id: str, graph: dict, timestamp: str) -> Path:
    """Save a graph snapshot to disk. Returns the written file path."""
    viz_dir = _CACHE_ROOT / viz_id
    viz_dir.mkdir(parents=True, exist_ok=True)
    path = viz_dir / f"{timestamp}.json"
    path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    logger.debug("Saved snapshot: %s", path)
    return path


def load_catalog() -> dict[str, list[dict]]:
    """Load all cached snapshots, grouped by viz-id.

    Returns ``{ viz_id: [snapshot_dict, ...] }`` sorted by timestamp.
    """
    catalog: dict[str, list[dict]] = {}
    if not _CACHE_ROOT.is_dir():
        return catalog

    for viz_dir in sorted(_CACHE_ROOT.iterdir()):
        if not viz_dir.is_dir() or viz_dir.name.startswith("."):
            continue
        viz_id = viz_dir.name
        snapshots = []
        for f in sorted(viz_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                snapshots.append(
                    {
                        "vizId": viz_id,
                        "timestamp": f.stem,
                        "graph": data,
                    }
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping invalid snapshot %s: %s", f, exc)
        if snapshots:
            catalog[viz_id] = snapshots
    return catalog


def remove_snapshot(viz_id: str, timestamp: str) -> bool:
    """Remove a specific snapshot. Returns True if deleted."""
    path = _CACHE_ROOT / viz_id / f"{timestamp}.json"
    if path.is_file():
        path.unlink()
        # Remove empty viz-id directory
        viz_dir = path.parent
        if viz_dir.is_dir() and not any(viz_dir.iterdir()):
            viz_dir.rmdir()
        return True
    return False


def write_server_info(port: int, pid: int) -> None:
    """Write server.json with current server info."""
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    server_json_path().write_text(
        json.dumps({"port": port, "pid": pid}),
        encoding="utf-8",
    )


def read_server_info() -> dict | None:
    """Read server.json. Returns None if missing or invalid."""
    path = server_json_path()
    if not path.is_file():
        return None
    try:
        result: dict = json.loads(path.read_text(encoding="utf-8"))
        return result
    except (json.JSONDecodeError, OSError):
        return None


def clear_server_info() -> None:
    """Remove server.json."""
    path = server_json_path()
    if path.is_file():
        path.unlink()
