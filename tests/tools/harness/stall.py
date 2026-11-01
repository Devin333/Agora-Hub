"""Server-side stalls that end when released or when the client goes away.

A fault handler that keeps waiting after its client timed out and closed the
socket would pile up one thread per stalled request, so the harness itself
would look like a leak. Every stall polls for the peer closing.
"""

from __future__ import annotations

import select
import socket
import ssl
import threading
import time
from typing import Literal

PEER_POLL_SECONDS = 0.05
StallOutcome = Literal["elapsed", "released", "peer_closed"]


def peer_closed(connection: socket.socket) -> bool:
    """True once the client has closed or reset its end of ``connection``.

    Called only after the request has been read; the client sends nothing
    more until it gives up, so new readable data means it is closing. TLS
    sockets cannot peek, so for them readability itself is the signal (a
    close_notify or TCP FIN).
    """

    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        if isinstance(connection, ssl.SSLSocket):
            return True
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def stall(released: threading.Event, connection: socket.socket, seconds: float) -> StallOutcome:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "elapsed"
        if released.wait(min(PEER_POLL_SECONDS, remaining)):
            return "released"
        if peer_closed(connection):
            return "peer_closed"
