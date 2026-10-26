from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from scripts.local_desktop.common import DesktopOperationError


MAX_STARTUP_CONTROL_BYTES = 256
_STD_INPUT_HANDLE = -10 & 0xFFFFFFFF
_STARTUP_CANCEL_MESSAGE = b'{"command":"cancel-start"}\n'


def parse_startup_control_message(raw: bytes | bytearray | str) -> str:
    """Accept only the canonical single-message private startup protocol."""

    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > MAX_STARTUP_CONTROL_BYTES or encoded != _STARTUP_CANCEL_MESSAGE:
        raise DesktopOperationError("desktop_start_control_invalid", "startup control message is not allowed")
    return "cancel-start"


class DesktopStartCancelled(DesktopOperationError):
    """Raised only at explicit startup safety checkpoints."""

    def __init__(self) -> None:
        super().__init__(
            "desktop_start_cancelled",
            "desktop startup was cancelled by the host before readiness was published",
            exit_code=4,
        )


@dataclass(frozen=True)
class StartupCancellation:
    _event: threading.Event

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


_ACTIVE_CANCELLATION: ContextVar[StartupCancellation | None] = ContextVar(
    "desktop_startup_cancellation",
    default=None,
)


def check_startup_cancellation() -> None:
    """Fail at a safe checkpoint when the desktop host requested cancellation.

    Calls outside the CLI-owned startup context intentionally do nothing so shared
    filesystem verification remains reusable by maintenance commands.
    """

    cancellation = _ACTIVE_CANCELLATION.get()
    if cancellation is not None and cancellation.requested:
        raise DesktopStartCancelled()


@contextmanager
def startup_cancellation_context(*, stream: Any | None = None) -> Iterator[StartupCancellation]:
    """Listen for the single private start control message on a daemon thread.

    The listener never writes stdout. Invalid or oversized controls are diagnosed
    generically on stderr without reflecting attacker-controlled input.

    Without an explicit ``stream`` the host's stdin pipe is the control channel.
    It is moved off the process standard input before the listener starts, so the
    start command owns stdin for the rest of the process lifetime (see
    ``_detach_host_control_channel``).
    """

    cancellation = StartupCancellation(threading.Event())
    context_closed = threading.Event()
    read_control = _control_reader(stream)
    token: Token[StartupCancellation | None] = _ACTIVE_CANCELLATION.set(cancellation)

    def listen() -> None:
        try:
            raw = read_control()
            if context_closed.is_set() or not raw:
                return
            command = parse_startup_control_message(raw)
            if command == "cancel-start":
                _diagnose("desktop startup cancellation requested")
                cancellation._event.set()
        except DesktopOperationError:
            if not context_closed.is_set():
                _diagnose("desktop startup control message was rejected")
        except (OSError, ValueError, TypeError):
            if not context_closed.is_set():
                _diagnose("desktop startup control channel became unavailable")

    listener = threading.Thread(target=listen, name="desktop-start-control", daemon=True)
    listener.start()
    try:
        yield cancellation
    finally:
        context_closed.set()
        _ACTIVE_CANCELLATION.reset(token)


def _control_reader(stream: Any | None) -> Callable[[], bytes]:
    if stream is not None:
        return lambda: _read_stream(stream)
    try:
        descriptor = _detach_host_control_channel()
    except OSError as exc:
        failure = exc

        def unavailable() -> bytes:
            raise failure

        return unavailable
    return lambda: _read_descriptor(descriptor)


def _detach_host_control_channel() -> int:
    """Move the host control pipe off fd 0 and return a private descriptor for it.

    The desktop host keeps the start command's stdin pipe open until it either
    sends ``cancel-start`` or the process exits. On Windows a synchronous read
    pending on an anonymous pipe serializes every other operation on that pipe,
    so any child that inherits it as standard input (Node and npm probe their
    standard handles at startup) blocks until the host writes or closes it. That
    stalled release verification at ``npm --version`` indefinitely.

    The pipe is therefore duplicated onto a non-inheritable descriptor owned by
    the listener, and fd 0 plus the Win32 standard input handle are pointed at
    the null device before any read starts. Children spawned during startup then
    inherit an empty stdin, while cancellation still arrives on the private
    descriptor. The private descriptor is never closed: closing a handle under a
    pending synchronous read can block, and process exit releases it.
    """

    descriptor = os.dup(0)
    try:
        null_descriptor = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(null_descriptor, 0)
        finally:
            os.close(null_descriptor)
        _publish_standard_input()
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _publish_standard_input() -> None:
    # subprocess and native children read STD_INPUT_HANDLE, not the CRT descriptor
    # table, so keep the Win32 standard handle in step with fd 0 explicitly.
    if os.name != "nt":
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
    kernel32.SetStdHandle.restype = wintypes.BOOL
    if not kernel32.SetStdHandle(_STD_INPUT_HANDLE, msvcrt.get_osfhandle(0)):
        raise ctypes.WinError(ctypes.get_last_error())


def _read_stream(stream: Any) -> bytes:
    raw = stream.read(MAX_STARTUP_CONTROL_BYTES + 1)
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    return str(raw).encode("utf-8")


def _read_descriptor(descriptor: int) -> bytes:
    # Do not use Python's buffered stdin here. A daemon blocked in BufferedReader.read()
    # can retain CPython's buffered-stdio lock during interpreter finalization and
    # abort an otherwise clean no-cancellation startup. os.read blocks only in the
    # OS on the private pipe descriptor and is safely discarded at process exit.
    chunks = bytearray()
    while len(chunks) <= MAX_STARTUP_CONTROL_BYTES:
        chunk = os.read(descriptor, MAX_STARTUP_CONTROL_BYTES + 1 - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _diagnose(message: str) -> None:
    encoded = f"{message}\n".encode("utf-8")
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(2, encoded[offset:])
            if written <= 0:
                return
            offset += written
    except OSError:
        pass


__all__ = [
    "DesktopStartCancelled",
    "MAX_STARTUP_CONTROL_BYTES",
    "StartupCancellation",
    "check_startup_cancellation",
    "parse_startup_control_message",
    "startup_cancellation_context",
]
