"""Scriptable local HTTP server that reproduces real upstream failures.

Each route holds an ordered script of steps. Requests consume the steps in
order and the final step repeats, so ``route("/x", Respond(503), Respond(200))``
models "fail once, then recover". Every request is recorded so tests can
assert retries, duplicate sends and header leakage.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Union
from urllib.parse import urlsplit

from tests.tools.harness.stall import StallOutcome, stall

MAX_STALL_SECONDS = 30.0
_CONNECTION_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes
    received_at: float

    def header(self, name: str) -> str | None:
        lowered = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == lowered:
                return value
        return None


@dataclass(frozen=True)
class Respond:
    status: int = 200
    body: bytes | str = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    content_type: str | None = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class Redirect:
    location: str
    status: int = 302


@dataclass(frozen=True)
class Delay:
    """Wait before running ``then``; ``release()`` cuts the wait short."""

    seconds: float
    then: "Step" = field(default_factory=Respond)


@dataclass(frozen=True)
class Hang:
    """Accept the request and never answer until the server is released."""


@dataclass(frozen=True)
class Trickle:
    """Send a valid response but stream the body one chunk at a time."""

    body: bytes | str
    chunk_size: int = 1
    interval_seconds: float = 0.05
    status: int = 200
    content_type: str | None = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class Truncate:
    """Declare the full Content-Length, send only part of the body, then close."""

    body: bytes | str
    sent_bytes: int
    status: int = 200
    content_type: str | None = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class Reset:
    """Abort the connection with a TCP reset before any response bytes."""


@dataclass(frozen=True)
class Drop:
    """Close the connection cleanly without sending a response."""


Step = Union[Respond, Redirect, Delay, Hang, Trickle, Truncate, Reset, Drop]


def _as_bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


class FaultHTTPServer:
    """Threaded HTTP server whose shutdown joins every handler thread."""

    def __init__(self) -> None:
        self._routes: dict[str, deque[Step]] = {}
        self._lock = threading.Lock()
        self._released = threading.Event()
        self._requests: list[RecordedRequest] = []
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fault = self
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def url(self, path: str = "/", *, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path if path.startswith('/') else '/' + path}"

    def route(self, path: str, *steps: Step) -> None:
        if not steps:
            raise ValueError("a route needs at least one step")
        with self._lock:
            self._routes[path] = deque(steps)

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._requests)

    def requests_for(self, path: str) -> list[RecordedRequest]:
        return [request for request in self.requests if request.path == path]

    def release(self) -> None:
        """Unblock every hanging or delayed handler."""

        self._released.set()

    def start(self) -> "FaultHTTPServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"fault-http:{self.port}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.release()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join()

    def __enter__(self) -> "FaultHTTPServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._requests.append(request)

    def _next_step(self, path: str) -> Step:
        with self._lock:
            steps = self._routes.get(path)
            if not steps:
                return Respond(404, b"no fault route", content_type="text/plain")
            return steps.popleft() if len(steps) > 1 else steps[0]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    # socketserver's default backlog of 5 drops bursts of parallel connects;
    # production servers listen with at least the common somaxconn of 128.
    request_queue_size = 128
    fault: FaultHTTPServer

    def handle_error(self, request: object, client_address: object) -> None:
        # Client-side disconnects are the point of these tests, not noise.
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: _Server

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_PUT(self) -> None:
        self._serve()

    def do_DELETE(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def _serve(self) -> None:
        fault = self.server.fault
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        fault._record(
            RecordedRequest(
                method=self.command,
                path=parts.path or "/",
                query=parts.query,
                headers={key: value for key, value in self.headers.items()},
                body=body,
                received_at=time.monotonic(),
            )
        )
        try:
            self._run(fault._next_step(parts.path or "/"), fault)
        except _CONNECTION_ERRORS:
            self.close_connection = True

    def _run(self, step: Step, fault: FaultHTTPServer) -> None:
        if isinstance(step, Respond):
            self._respond(step.status, _as_bytes(step.body), step.content_type, step.headers)
        elif isinstance(step, Redirect):
            self._respond(step.status, b"", None, {"Location": step.location})
        elif isinstance(step, Delay):
            if self._stall(fault, step.seconds) != "peer_closed":
                self._run(step.then, fault)
        elif isinstance(step, Hang):
            self._stall(fault, MAX_STALL_SECONDS)
        elif isinstance(step, Trickle):
            self._trickle(step, fault)
        elif isinstance(step, Truncate):
            body = _as_bytes(step.body)
            self._headers(step.status, len(body), step.content_type, {})
            self.wfile.write(body[: step.sent_bytes])
        elif isinstance(step, Reset):
            self._reset()
        elif isinstance(step, Drop):
            self.close_connection = True
        else:  # pragma: no cover - Step is a closed union
            raise TypeError(f"unknown fault step: {step!r}")

    def _headers(
        self,
        status: int,
        content_length: int,
        content_type: str | None,
        headers: Mapping[str, str],
    ) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str | None,
        headers: Mapping[str, str],
    ) -> None:
        self._headers(status, len(body), content_type, headers)
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _trickle(self, step: Trickle, fault: FaultHTTPServer) -> None:
        body = _as_bytes(step.body)
        self._headers(step.status, len(body), step.content_type, {})
        size = max(1, step.chunk_size)
        for start in range(0, len(body), size):
            self.wfile.write(body[start : start + size])
            outcome = self._stall(fault, step.interval_seconds)
            if outcome == "peer_closed":
                return
            if outcome == "released":
                self.wfile.write(body[start + size :])
                return

    def _stall(self, fault: FaultHTTPServer, seconds: float) -> StallOutcome:
        return stall(fault._released, self.connection, min(seconds, MAX_STALL_SECONDS))

    def _reset(self) -> None:
        # Windows packs struct linger as two u_shorts, POSIX as two ints.
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        self.close_connection = True
        self.rfile.close()
        self.wfile.close()
        self.connection.close()
