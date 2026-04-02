"""Minimal HTTP server for the launch-plus visualizer.

Serves the single-page app with graph data embedded as JSON.
Uses only stdlib — no external dependencies.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import webbrowser
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from importlib.resources import files
from pathlib import Path

from launch_plus.visualizer.graph import actions_to_graph

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(str(files("launch_plus.visualizer") / "static"))


class _Handler(SimpleHTTPRequestHandler):
    """Request handler that serves index.html with injected graph JSON."""

    def __init__(self, *args, graph_json: str = "{}", **kwargs):
        self._graph_json = graph_json
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_index()
        elif self.path.startswith("/static/"):
            self._serve_static()
        else:
            self.send_error(404)

    def _serve_index(self):
        html_path = _STATIC_DIR / "index.html"
        html = html_path.read_text(encoding="utf-8")
        html = html.replace("__GRAPH_JSON__", self._graph_json)
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_static(self):
        # Strip "/static/" prefix
        rel = self.path[len("/static/") :]
        file_path = _STATIC_DIR / rel
        if not file_path.is_file() or not str(file_path.resolve()).startswith(
            str(_STATIC_DIR.resolve())
        ):
            self.send_error(404)
            return

        content = file_path.read_bytes()
        content_type = _guess_content_type(file_path.suffix)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        # Suppress default stderr logging for each request
        pass


def _guess_content_type(suffix: str) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
    }.get(suffix, "application/octet-stream")


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
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Start the visualizer server and optionally open a browser.

    Blocks until interrupted with Ctrl+C.
    """
    graph = actions_to_graph(actions, package, launcher)
    graph_json = json.dumps(graph, ensure_ascii=False)

    if port == 0:
        port = _find_free_port()

    handler_class = partial(_Handler, graph_json=graph_json)
    server = HTTPServer(("127.0.0.1", port), handler_class)
    url = f"http://127.0.0.1:{port}"

    print(f"launch-plus visualizer: {url}", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down visualizer.", file=sys.stderr)
    finally:
        server.server_close()
