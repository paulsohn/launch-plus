"""Visualizer server: stdlib HTTP serving + REST API.

Serves the built SPA from ``roscope/visualizer/static/`` and
provides ``/api/catalog`` for the frontend to poll cached snapshots.
No external dependencies — uses only ``http.server``.

The server runs as a background (daemon) process. A watchdog thread
shuts it down automatically when no browser has polled for a while.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path

from roscope.visualizer import cache

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(str(files("roscope.visualizer") / "static"))

# Auto-shutdown: if no /api/catalog poll for this many seconds
# after at least one poll has been received, the server exits.
_IDLE_TIMEOUT_SECONDS = 10
_WATCHDOG_CHECK_INTERVAL = 3


# ── Shared state for the watchdog ─────────────────────────────────────

_last_poll_time: float = 0.0
_ever_polled: bool = False
_poll_lock = threading.Lock()


def _record_poll() -> None:
    global _last_poll_time, _ever_polled
    with _poll_lock:
        _last_poll_time = time.monotonic()
        _ever_polled = True


# ── HTTP handler ──────────────────────────────────────────────────────


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
        _record_poll()
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


# ── Watchdog thread ───────────────────────────────────────────────────


def _watchdog(server: HTTPServer) -> None:
    """Shut down the server when no browser has polled for a while."""
    while True:
        time.sleep(_WATCHDOG_CHECK_INTERVAL)
        with _poll_lock:
            if _ever_polled and (time.monotonic() - _last_poll_time) > _IDLE_TIMEOUT_SECONDS:
                break
    cache.clear_server_info()
    server.shutdown()


# ── Public API ────────────────────────────────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port: int = s.getsockname()[1]
        return port


def _run_server(port: int) -> None:
    """Entry point for the background server process."""
    # Detach stdio so the parent shell isn't held open
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    server = HTTPServer(("127.0.0.1", port), _Handler)

    # Start watchdog thread (daemon — dies with the process)
    wd = threading.Thread(target=_watchdog, args=(server,), daemon=True)
    wd.start()

    try:
        server.serve_forever()
    finally:
        server.server_close()
        cache.clear_server_info()


def serve(
    actions: list,
    package: str,
    launcher: str,
    *,
    viz_id: str = "",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Save graph to cache, start background server if needed, open browser.

    Returns immediately — the server runs in a forked child process.
    """
    from roscope.visualizer.graph import actions_to_graph

    # Build graph and save to cache
    graph = actions_to_graph(actions, package, launcher)
    timestamp = graph["metadata"]["timestamp"]

    if not viz_id:
        viz_id = "default"
    viz_id = cache.sanitize_viz_id(viz_id)

    cache.save_snapshot(viz_id, graph, timestamp)

    # Check if a server is already running
    info = cache.read_server_info()
    if info is not None:
        existing_port = info.get("port")
        existing_pid = info.get("pid")
        if existing_port and _is_server_alive(existing_pid):
            url = f"http://127.0.0.1:{existing_port}"
            print(f"Visualizer: {url} (server already running)", file=sys.stderr)
            if open_browser:
                webbrowser.open(url)
            return

    # Pick a port before forking so the parent can report it
    if port == 0:
        port = _find_free_port()

    url = f"http://127.0.0.1:{port}"

    # Fork a child process for the server
    pid = os.fork()
    if pid == 0:
        # ── Child: become a daemon ──
        os.setsid()  # new session, detach from terminal
        cache.write_server_info(port, os.getpid())
        _run_server(port)
        os._exit(0)
    else:
        # ── Parent: report and return to shell ──
        print(f"Visualizer: {url} (server pid {pid})", file=sys.stderr)
        if open_browser:
            webbrowser.open(url)
        # Don't waitpid — let the child run independently


def _is_server_alive(pid: int | None) -> bool:
    """Check if a process with the given PID is still running."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
