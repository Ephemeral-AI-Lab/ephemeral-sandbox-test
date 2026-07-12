"""Minimal loopback-only HTTP adapter for the Control Room API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from harness.api.server import ApiRequest, ControlRoomApi, MAX_BODY_BYTES


class LoopbackControlRoomServer(ThreadingHTTPServer):
    """A bound HTTP server; callers own ``serve_forever`` and ``shutdown``."""

    daemon_threads = True


def make_loopback_server(api: ControlRoomApi, host: str, port: int) -> LoopbackControlRoomServer:
    """Bind only a numeric loopback address and adapt requests without CORS."""

    try:
        if not ip_address(host).is_loopback:
            raise ValueError
    except ValueError as error:
        raise ValueError("Control Room may bind only a numeric loopback address") from error

    class Handler(_Handler):
        controller = api

    return LoopbackControlRoomServer((host, port), Handler)


class _Handler(BaseHTTPRequestHandler):
    controller: ControlRoomApi
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = MAX_BODY_BYTES + 1
        body = self.rfile.read(min(content_length, MAX_BODY_BYTES + 1)) if content_length else b""
        response = self.controller.handle(ApiRequest(self.command, self.path, dict(self.headers.items()), body))
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid sending request data to a console or durable log."""
