"""A local HTTPS origin with a certificate for a made-up public hostname.

Tests point that hostname at 127.0.0.1 through an injected resolver while the
policy is told the hostname resolves to a public address, so the pinned
connection, SNI and certificate verification all run for real without leaving
the machine.
"""

from __future__ import annotations

import datetime
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ORIGIN_HOST = "models.example.com"


def _self_signed(directory: Path, hostname: str) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "origin.pem"
    key_path = directory / "origin.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def _reply(self, status: int, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: bytes) -> None:
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "headers": {name.casefold(): value for name, value in self.headers.items()},
                "body": body,
                "sni": getattr(self.connection, "server_hostname", None),
            }
        )

    def do_GET(self) -> None:
        self._record(b"")
        if self.path.startswith("/redirect"):
            self._reply(302, b"", headers={"Location": "https://internal.example.com/secret"})
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for line in (b"data: one\n\n", b"data: two\n\n"):
                self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/big":
            self._reply(200, b"x" * 4096)
        elif self.path == "/missing":
            self._reply(404, b'{"error":"nope"}', headers={"Content-Type": "application/json"})
        else:
            self._reply(200, json.dumps({"ok": True, "path": self.path}).encode(), headers={"Content-Type": "application/json"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self._record(body)
        self._reply(200, json.dumps({"echo": body.decode()}).encode(), headers={"Content-Type": "application/json"})


@pytest.fixture
def tls_origin(tmp_path):
    cert_path, key_path = _self_signed(tmp_path, ORIGIN_HOST)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []  # type: ignore[attr-defined]
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    sni_seen: list[str | None] = []
    server_context.sni_callback = lambda sock, name, _ctx: sni_seen.append(name)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(cert_path))
    try:
        yield _Origin(server, client_context, sni_seen)
    finally:
        server.shutdown()
        server.server_close()


class _Origin:
    def __init__(self, server: ThreadingHTTPServer, client_context: ssl.SSLContext, sni_seen: list) -> None:
        self.server = server
        self.port = server.server_address[1]
        self.client_context = client_context
        self.sni_seen = sni_seen

    @property
    def requests(self) -> list[dict]:
        return self.server.requests  # type: ignore[attr-defined]

    def url(self, path: str = "/") -> str:
        return f"https://{ORIGIN_HOST}:{self.port}{path}"


PUBLIC_ANSWER = "93.184.215.14"


@pytest.fixture
def dialed(monkeypatch) -> list[tuple[str, int]]:
    """Route connections to ``PUBLIC_ANSWER`` to the local origin and record every dial.

    The policy sees a public answer for ``ORIGIN_HOST``; the socket layer then
    reaches 127.0.0.1 for it. Anything else dialed is refused, so a transport
    that resolved the name a second time would fail the test.
    """
    import socket

    real_create_connection = socket.create_connection
    calls: list[tuple[str, int]] = []

    def create_connection(address, *args, **kwargs):
        host, port = address
        calls.append((host, port))
        if host != PUBLIC_ANSWER:
            raise ConnectionRefusedError(f"test refused unexpected dial to {host}")
        return real_create_connection(("127.0.0.1", port), *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    return calls


__all__ = ["ORIGIN_HOST", "PUBLIC_ANSWER"]
