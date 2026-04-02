"""Visualizer server: stdlib HTTP serving + REST API.

Serves the built SPA from ``roscope/visualizer/static/`` and
provides ``/api/catalog`` for the frontend to poll cached snapshots.
No external dependencies — uses only ``http.server``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path

from roscope.visualizer import cache

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(str(files("roscope.visualizer") / "static"))


class _Handler(BaseHTTPRequestHandler):
    """Serves static files and the catalog API."""

    def do_GET(self) -> None:
        path = self.path.split("?")[0]  # strip query string

        if path == "/api/catalog":
            self._serve_catalog()
        elif path == "/api/remove":
            self.send_error(405, "Use DELETE")
        elif path == "/" or path == "/index.html":
            self._serve_file(_STATIC_DIR / "index.html")
        elif path.startswith("/assets/"):
            self._serve_file(_STATIC_DIR / path.lstrip("/"))
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/remove":
            self._handle_remove()
        else:
            self.send_error(404)

    def _serve_catalog(self) -> None:
        catalog = cache.load_catalog()
        body = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_remove(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self.send_error(400, "Missing body")
            return
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        viz_id = body.get("vizId", "")
        timestamp = body.get("timestamp", "")
        if not viz_id or not timestamp:
            self.send_error(400, "Missing vizId or timestamp")
            return

        removed = cache.remove_snapshot(viz_id, timestamp)
        resp = json.dumps({"removed": removed}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _serve_file(self, file_path: Path) -> None:
        # Security: ensure path doesn't escape static dir
        try:
            resolved = file_path.resolve()
            if not str(resolved).startswith(str(_STATIC_DIR.resolve())):
                self.send_error(403)
                return
        except (OSError, ValueError):
            self.send_error(404)
            return

        if not resolved.is_file():
            self.send_error(404)
            return

        content = resolved.read_bytes()
        content_type, _ = mimetypes.guess_type(str(resolved))
        if content_type is None:
            content_type = "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress default per-request stderr logging
        pass


# ── Public API ────────────────────────────────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port: int = s.getsockname()[1]
        return port


def serve(
    actions: list,
    package: str,
    launcher: str,
    *,
    viz_id: str = "",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Save graph to cache, start server if not already running, open browser.

    This is the main entry point called by the CLI.
    """
    from roscope.visualizer.graph import actions_to_graph

    # Build graph and save to cache
    graph = actions_to_graph(actions, package, launcher)
    timestamp = graph["metadata"]["timestamp"]

    if not viz_id:
        viz_id = f"{package}/{launcher}"

    cache.save_snapshot(viz_id, graph, timestamp)

    # Check if a server is already running
    info = cache.read_server_info()
    if info is not None:
        existing_port = info.get("port")
        existing_pid = info.get("pid")
        if existing_port and _is_server_alive(existing_pid):
            # Server is running — browser will pick up new snapshot via polling
            print(
                f"Visualizer server already running at http://127.0.0.1:{existing_port}",
                file=sys.stderr,
            )
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{existing_port}")
            return

    # Start new server
    if port == 0:
        port = _find_free_port()

    pid = os.getpid()
    cache.write_server_info(port, pid)

    server = HTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"

    print(f"roscope visualizer: {url}", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down visualizer.", file=sys.stderr)
    finally:
        cache.clear_server_info()
        server.server_close()


def _is_server_alive(pid: int | None) -> bool:
    """Check if a process with the given PID is still running."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
