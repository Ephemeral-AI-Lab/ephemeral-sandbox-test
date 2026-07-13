"""Minimal loopback-only HTTP adapter for the Control Room API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import mimetypes
from pathlib import Path
import socket
from urllib.parse import unquote, urlsplit

from harness.api.server import ApiRequest, ControlRoomApi, MAX_BODY_BYTES


class LoopbackControlRoomServer(ThreadingHTTPServer):
    """A bound HTTP server; callers own ``serve_forever`` and ``shutdown``."""

    daemon_threads = True


def make_loopback_server(
    api: ControlRoomApi,
    host: str,
    port: int,
    *,
    web_root: Path | None = None,
) -> LoopbackControlRoomServer:
    """Bind only loopback and serve the API and optional built SPA together."""

    try:
        address = ip_address(host)
        if not address.is_loopback:
            raise ValueError
    except ValueError as error:
        raise ValueError("Control Room may bind only a numeric loopback address") from error
    static_root = _web_root(web_root) if web_root is not None else None

    class Handler(_Handler):
        controller = api
        web_root = static_root

    server_type = LoopbackControlRoomServer
    if address.version == 6:
        server_type = type("IPv6LoopbackControlRoomServer", (LoopbackControlRoomServer,), {"address_family": socket.AF_INET6})
    return server_type((host, port), Handler)


class _Handler(BaseHTTPRequestHandler):
    controller: ControlRoomApi
    web_root: Path | None = None
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        if self.command == "GET" and self.web_root is not None and not urlsplit(self.path).path.startswith("/api/v1/"):
            self._serve_web()
            return
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

    def _serve_web(self) -> None:
        if self.headers.get("Host") != self.controller.expected_host:
            self._send_bytes(421, b"Misdirected Request\n", "text/plain; charset=utf-8")
            return
        path = unquote(urlsplit(self.path).path)
        if path in {"/", "/e2e"} or path.startswith("/e2e/"):
            self._send_file(self.web_root / "index.html", "no-store", "text/html")
            return
        if path.startswith("/assets/"):
            relative = Path(path.removeprefix("/"))
            if ".." not in relative.parts:
                self._send_file(self.web_root / relative, "public, max-age=31536000, immutable")
                return
        self._send_bytes(404, b"Not Found\n", "text/plain; charset=utf-8")

    def _send_file(self, candidate: Path, cache_control: str, media_type: str | None = None) -> None:
        assert self.web_root is not None
        try:
            target = candidate.resolve(strict=True)
            if not target.is_relative_to(self.web_root) or not target.is_file() or candidate.is_symlink():
                raise OSError
            content = target.read_bytes()
        except OSError:
            self._send_bytes(404, b"Not Found\n", "text/plain; charset=utf-8")
            return
        self._send_bytes(
            200,
            content,
            media_type or mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            cache_control=cache_control,
        )

    def _send_bytes(self, status: int, body: bytes, media_type: str, *, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid sending request data to a console or durable log."""


def _web_root(value: Path) -> Path:
    root = value.expanduser().resolve(strict=True)
    if not root.is_dir() or not (root / "index.html").is_file():
        raise ValueError("Control Room web root must contain index.html")
    return root
