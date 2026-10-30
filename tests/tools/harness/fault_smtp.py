"""Scriptable local SMTP server for notification.email failure scenarios.

Speaks enough of RFC 5321 for ``smtplib`` (EHLO/HELO, AUTH PLAIN, MAIL, RCPT,
DATA, RSET, NOOP, QUIT) and can refuse service, drop the session, refuse
STARTTLS, reject recipients, fail DATA, or stall at a chosen point. A stall
at ``data_end`` records the message before going silent, which is the
"delivered but never acknowledged" case a client cannot distinguish from loss.
"""

from __future__ import annotations

import base64
import socketserver
import threading
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Literal, cast

from tests.tools.harness.stall import stall

MAX_STALL_SECONDS = 30.0

StallPoint = Literal["greeting", "data_command", "data_end"]


@dataclass(frozen=True)
class SMTPScript:
    refuse_service: bool = False
    drop_after_ehlo: bool = False
    advertise_starttls: bool = False
    credentials: tuple[str, str] | None = None
    rejected_recipients: frozenset[str] = frozenset()
    data_reply: tuple[int, str] = (250, "queued")
    stall_at: StallPoint | None = None


@dataclass(frozen=True)
class ReceivedMessage:
    mail_from: str
    recipients: tuple[str, ...]
    data: bytes

    @property
    def message(self) -> EmailMessage:
        return cast(EmailMessage, message_from_bytes(self.data, policy=policy.default))


@dataclass
class _Session:
    mail_from: str | None = None
    recipients: list[str] = field(default_factory=list)
    authenticated: bool = False


class FaultSMTPServer:
    def __init__(self, script: SMTPScript | None = None) -> None:
        self.script = script or SMTPScript()
        self._lock = threading.Lock()
        self._released = threading.Event()
        self._messages: list[ReceivedMessage] = []
        self._commands: list[str] = []
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fault = self
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def messages(self) -> list[ReceivedMessage]:
        with self._lock:
            return list(self._messages)

    @property
    def commands(self) -> list[str]:
        with self._lock:
            return list(self._commands)

    def release(self) -> None:
        self._released.set()

    def start(self) -> "FaultSMTPServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"fault-smtp:{self.port}",
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

    def __enter__(self) -> "FaultSMTPServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _record_command(self, line: str) -> None:
        with self._lock:
            self._commands.append(line)

    def _record_message(self, message: ReceivedMessage) -> None:
        with self._lock:
            self._messages.append(message)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 128
    fault: FaultSMTPServer

    def handle_error(self, request: object, client_address: object) -> None:
        return None


class _Handler(socketserver.StreamRequestHandler):
    timeout = MAX_STALL_SECONDS
    server: _Server

    def handle(self) -> None:
        try:
            self._converse()
        except (OSError, ConnectionError):
            return

    def _send(self, code: int, text: str) -> None:
        self.wfile.write(f"{code} {text}\r\n".encode("utf-8"))

    def _send_multiline(self, code: int, lines: list[str]) -> None:
        for index, line in enumerate(lines):
            separator = " " if index == len(lines) - 1 else "-"
            self.wfile.write(f"{code}{separator}{line}\r\n".encode("utf-8"))

    def _converse(self) -> None:
        fault = self.server.fault
        script = fault.script
        if script.stall_at == "greeting":
            stall(fault._released, self.connection, MAX_STALL_SECONDS)
            return
        if script.refuse_service:
            self._send(554, "fault server refuses service")
            return
        self._send(220, "fault-smtp ready")
        session = _Session()
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            fault._record_command(line)
            verb, _, argument = line.partition(" ")
            verb = verb.upper()
            if verb in {"EHLO", "HELO"}:
                features = ["fault-smtp"]
                if verb == "EHLO":
                    if script.advertise_starttls:
                        features.append("STARTTLS")
                    if script.credentials is not None:
                        features.append("AUTH PLAIN")
                self._send_multiline(250, features)
                if script.drop_after_ehlo:
                    return
            elif verb == "STARTTLS":
                self._send(454, "TLS not available on the fault server")
            elif verb == "AUTH":
                session.authenticated = self._authenticate(argument, script)
            elif verb == "MAIL":
                if script.credentials is not None and not session.authenticated:
                    self._send(530, "authentication required")
                    continue
                session.mail_from = _address(argument)
                session.recipients = []
                self._send(250, "sender ok")
            elif verb == "RCPT":
                recipient = _address(argument)
                if recipient in script.rejected_recipients:
                    self._send(550, f"mailbox unavailable: {recipient}")
                else:
                    session.recipients.append(recipient)
                    self._send(250, "recipient ok")
            elif verb == "DATA":
                if not self._data(session, script):
                    return
            elif verb == "RSET":
                session.mail_from = None
                session.recipients = []
                self._send(250, "reset")
            elif verb == "NOOP":
                self._send(250, "ok")
            elif verb == "QUIT":
                self._send(221, "bye")
                return
            else:
                self._send(502, "command not implemented")

    def _authenticate(self, argument: str, script: SMTPScript) -> bool:
        mechanism, _, initial = argument.partition(" ")
        if mechanism.upper() != "PLAIN":
            self._send(504, "unsupported mechanism")
            return False
        if not initial:
            self._send(334, "")
            initial = self.rfile.readline().decode("utf-8", errors="replace").strip()
        try:
            _, username, password = base64.b64decode(initial).decode("utf-8").split("\0")
        except (ValueError, UnicodeDecodeError):
            self._send(501, "malformed credentials")
            return False
        if script.credentials == (username, password):
            self._send(235, "authenticated")
            return True
        self._send(535, "authentication failed")
        return False

    def _data(self, session: _Session, script: SMTPScript) -> bool:
        fault = self.server.fault
        if session.mail_from is None or not session.recipients:
            self._send(503, "need MAIL and RCPT first")
            return True
        if script.stall_at == "data_command":
            stall(fault._released, self.connection, MAX_STALL_SECONDS)
            return False
        self._send(354, "end data with <CR><LF>.<CR><LF>")
        lines: list[bytes] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return False
            if raw in {b".\r\n", b".\n"}:
                break
            lines.append(raw[1:] if raw.startswith(b"..") else raw)
        fault._record_message(
            ReceivedMessage(
                mail_from=session.mail_from,
                recipients=tuple(session.recipients),
                data=b"".join(lines),
            )
        )
        if script.stall_at == "data_end":
            stall(fault._released, self.connection, MAX_STALL_SECONDS)
            return False
        code, text = script.data_reply
        self._send(code, text)
        return True


def _address(argument: str) -> str:
    _, _, value = argument.partition(":")
    value = value.strip()
    if value.startswith("<"):
        value = value[1:].split(">", 1)[0]
    return value.split(" ", 1)[0]
