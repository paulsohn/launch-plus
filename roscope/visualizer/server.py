"""Visualizer server: WebSocket + HTTP static file serving.

Serves the built SPA from ``roscope/visualizer/static/`` and
provides a WebSocket endpoint at ``/ws`` for pushing graph snapshots
to connected browsers.

Server lifecycle:
    IDLE      (0 clients, never had any)  -> keep running, waiting for browser
    ACTIVE    (>0 clients)                -> serving
    DRAINING  (0 clients, had >0)         -> start shutdown grace period
    SHUTDOWN                              -> clean up, exit

The grace period (default 5 s) prevents shutdown during page reloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import signal
import socket
import sys
import webbrowser
from importlib.resources import files
from pathlib import Path

import websockets.asyncio.server
import websockets.datastructures
import websockets.http11
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as ws_serve

from roscope.visualizer import cache

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(str(files("roscope.visualizer") / "static"))

_SHUTDOWN_GRACE_SECONDS = 5


# ── Server state ──────────────────────────────────────────────────────


class _VizServer:
    """Manages WebSocket connections and the auto-shutdown lifecycle."""

    def __init__(self) -> None:
        self.clients: set[ServerConnection] = set()
        self._ever_had_clients = False
        self._shutdown_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def register(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        self._ever_had_clients = True
        if self._shutdown_task is not None:
            self._shutdown_task.cancel()
            self._shutdown_task = None
            logger.debug("Cancelled shutdown — new client connected.")

    async def unregister(self, ws: ServerConnection) -> None:
        self.clients.discard(ws)
        if self._ever_had_clients and len(self.clients) == 0:
            logger.debug(
                "All clients disconnected. Shutting down in %ds...",
                _SHUTDOWN_GRACE_SECONDS,
            )
            self._shutdown_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        await asyncio.sleep(_SHUTDOWN_GRACE_SECONDS)
        logger.info("Grace period elapsed — shutting down.")
        self._stop_event.set()

    async def broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(message, ensure_ascii=False)
        await asyncio.gather(
            *(ws.send(data) for ws in self.clients),
            return_exceptions=True,
        )

    async def wait_for_stop(self) -> None:
        await self._stop_event.wait()


# ── WebSocket handler ─────────────────────────────────────────────────


async def _ws_handler(ws: ServerConnection, server: _VizServer) -> None:
    await server.register(ws)
    try:
        # Send current catalog on connect
        catalog = cache.load_catalog()
        await ws.send(
            json.dumps(
                {"type": "catalog", "snapshots": catalog},
                ensure_ascii=False,
            )
        )

        # Listen for client messages
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "remove":
                viz_id = msg.get("vizId", "")
                timestamp = msg.get("timestamp", "")
                if cache.remove_snapshot(viz_id, timestamp):
                    await server.broadcast(
                        {
                            "type": "removed",
                            "vizId": viz_id,
                            "timestamp": timestamp,
                        }
                    )
            elif msg_type == "snapshot":
                # Forwarded from another CLI instance — broadcast to all browsers
                await server.broadcast(msg)
    finally:
        await server.unregister(ws)


# ── HTTP handler (aiohttp-free, using websockets' built-in) ──────────


async def _http_handler(
    connection: ServerConnection,
    request: websockets.http11.Request,
) -> websockets.http11.Response | None:
    """Process handler for websockets library — serves static files.

    Returning a Response rejects the WebSocket upgrade and sends HTTP instead.
    Returning None allows the WebSocket handshake to proceed.
    """
    path = request.path

    # Let WebSocket connections through
    if path == "/ws":
        return None

    # Serve static files
    if path == "/" or path == "/index.html":
        file_path = _STATIC_DIR / "index.html"
    elif path.startswith("/assets/"):
        file_path = _STATIC_DIR / path.lstrip("/")
    else:
        return websockets.http11.Response(
            404, "Not Found", websockets.datastructures.Headers(), b"Not Found"
        )

    # Security: ensure path doesn't escape static dir
    try:
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(_STATIC_DIR.resolve())):
            return websockets.http11.Response(
                403, "Forbidden", websockets.datastructures.Headers(), b"Forbidden"
            )
    except (OSError, ValueError):
        return websockets.http11.Response(
            404, "Not Found", websockets.datastructures.Headers(), b"Not Found"
        )

    if not file_path.is_file():
        return websockets.http11.Response(
            404, "Not Found", websockets.datastructures.Headers(), b"Not Found"
        )

    content = file_path.read_bytes()
    content_type, _ = mimetypes.guess_type(str(file_path))
    if content_type is None:
        content_type = "application/octet-stream"

    headers = websockets.datastructures.Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(content))
    return websockets.http11.Response(200, "OK", headers, content)


# ── Public API ────────────────────────────────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port: int = s.getsockname()[1]
        return port


async def _run_server(port: int, *, open_browser: bool = True) -> None:
    server_state = _VizServer()

    async with ws_serve(
        lambda ws: _ws_handler(ws, server_state),
        "127.0.0.1",
        port,
        process_request=_http_handler,
    ):
        url = f"http://127.0.0.1:{port}"
        pid = os.getpid()

        cache.write_server_info(port, pid)
        print(f"roscope visualizer: {url}", file=sys.stderr)
        print("Press Ctrl+C to stop.", file=sys.stderr)

        if open_browser:
            webbrowser.open(url)

        # Wait for either auto-shutdown or signal
        loop = asyncio.get_running_loop()
        stop_signal = asyncio.Event()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_signal.set)

        done, _ = await asyncio.wait(
            [
                asyncio.create_task(server_state.wait_for_stop()),
                asyncio.create_task(stop_signal.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

    cache.clear_server_info()
    print("\nVisualizer shut down.", file=sys.stderr)


def serve(
    actions: list,
    package: str,
    launcher: str,
    *,
    viz_id: str = "",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Save graph to cache, start server if needed, open browser.

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
            # Notify the running server via WebSocket
            _notify_running_server(existing_port, viz_id, timestamp, graph)
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{existing_port}")
            return

    # Start new server
    if port == 0:
        port = _find_free_port()

    asyncio.run(_run_server(port, open_browser=open_browser))


def _is_server_alive(pid: int | None) -> bool:
    """Check if a process with the given PID is still running."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _notify_running_server(
    port: int,
    viz_id: str,
    timestamp: str,
    graph: dict,
) -> None:
    """Connect to the running server's WS and push a snapshot notification."""
    import websockets.sync.client

    snapshot = {"vizId": viz_id, "timestamp": timestamp, "graph": graph}
    msg = json.dumps(
        {"type": "snapshot", "vizId": viz_id, "snapshot": snapshot},
        ensure_ascii=False,
    )
    try:
        with websockets.sync.client.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            ws.send(msg)
    except Exception as exc:
        logger.warning("Could not notify running server: %s", exc)
        print(
            f"Warning: could not notify running visualizer server: {exc}",
            file=sys.stderr,
        )
