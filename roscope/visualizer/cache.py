"""Local disk cache for visualizer snapshots.

Layout:
    ~/.cache/roscope-viz/
        server.json              # Running server info: { "port": ..., "pid": ... }
        <viz-id>/
            <timestamp>.json     # Snapshot: { "metadata": ..., "nodes": ..., ... }
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_env_cache = os.environ.get("ROSCOPE_VIZ_CACHE", "")
_CACHE_ROOT = Path(_env_cache) if _env_cache else (Path.home() / ".cache" / "roscope-viz")


def cache_root() -> Path:
    return _CACHE_ROOT


def server_json_path() -> Path:
    return _CACHE_ROOT / "server.json"


def sanitize_viz_id(viz_id: str) -> str:
    """Replace unsafe characters so viz-id is a valid flat directory name."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", viz_id)


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
    Viz-ids are flat sanitized directory names (see ``sanitize_viz_id``).
    """
    catalog: dict[str, list[dict]] = {}
    if not _CACHE_ROOT.is_dir():
        return catalog

    # Walk all .json files (except server.json at root) and reconstruct
    # the viz-id from the relative path of the parent directory.
    for f in sorted(_CACHE_ROOT.rglob("*.json")):
        if f.parent == _CACHE_ROOT:
            continue  # skip server.json and other root-level files
        viz_id = str(f.parent.relative_to(_CACHE_ROOT))
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping invalid snapshot %s: %s", f, exc)
            continue
        catalog.setdefault(viz_id, []).append(
            {
                "vizId": viz_id,
                "timestamp": f.stem,
                "graph": data,
            }
        )
    return catalog


def _safe_timestamp(timestamp: str) -> bool:
    """Return True when timestamp is a safe single-component filename stem."""
    if not timestamp or timestamp in {".", ".."}:
        return False
    return Path(timestamp).name == timestamp


def remove_snapshot(viz_id: str, timestamp: str) -> bool:
    """Remove a specific snapshot. Returns True if deleted."""
    viz_id = sanitize_viz_id(viz_id)
    if not _safe_timestamp(timestamp):
        logger.warning("Refusing to remove snapshot with unsafe timestamp: %r", timestamp)
        return False

    cache_root_resolved = _CACHE_ROOT.resolve(strict=False)
    path = (_CACHE_ROOT / viz_id / f"{timestamp}.json").resolve(strict=False)
    try:
        path.relative_to(cache_root_resolved)
    except ValueError:
        logger.warning("Refusing to delete snapshot outside cache root: %s", path)
        return False

    if path.is_file():
        path.unlink()
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
