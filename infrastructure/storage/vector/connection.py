"""The one way this codebase opens a Qdrant client.

qdrant-client talks REST through httpx, and httpx (``trust_env=True``) takes
its proxy from the environment, which on Windows includes the system proxy
from the registry. httpx does not apply the registry's bypass list
(``ProxyOverride``), so with a local proxy such as Clash on ``127.0.0.1:7890``
every request to a local Qdrant goes to the proxy and comes back
``502 Bad Gateway``. This module decides per Qdrant URL: a host that is
loopback, or that the platform says bypasses the proxy (``NO_PROXY`` /
``no_proxy``, or the registry's bypass list on Windows), is connected to
directly; anything else keeps the environment's proxy settings, so a remote
Qdrant behind a corporate proxy still works.

qdrant-client's own client/server version check is a bare ``httpx.get`` that
ignores ``trust_env``, so for a direct connection it would still go to the
proxy and warn that the server version is unknown. A direct connection
therefore runs the same check itself, through the client's configured
transport.
"""

from __future__ import annotations

import importlib.metadata
import ipaddress
import threading
import warnings
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit
from urllib.request import proxy_bypass

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


def connect_qdrant(
    url: str,
    *,
    api_key: str | None = None,
    timeout: int | None = None,
) -> "QdrantClient":
    from qdrant_client import QdrantClient

    options: dict[str, object] = {"url": url}
    if api_key is not None:
        options["api_key"] = api_key
    if timeout is not None:
        options["timeout"] = timeout
    direct = qdrant_bypasses_proxy(url)
    if direct:
        options["trust_env"] = False
        options["check_compatibility"] = False
    client = QdrantClient(**options)
    if direct:
        threading.Thread(
            target=_warn_if_incompatible,
            args=(client,),
            name="qdrant-version-check",
            daemon=True,
        ).start()
    return client


def qdrant_bypasses_proxy(
    url: str,
    *,
    bypass: Callable[[str], object] | None = None,
) -> bool:
    """Whether a client for ``url`` must connect directly instead of through the proxy.

    ``bypass`` answers for non-loopback hosts; it defaults to the platform's
    ``urllib.request.proxy_bypass``.
    """

    host = urlsplit(url).hostname
    if not host:
        return False
    if host.casefold() == "localhost" or _is_loopback(host):
        return True
    return bool((bypass or proxy_bypass)(host))


def _warn_if_incompatible(client: "QdrantClient") -> None:
    from qdrant_client.common.version_check import is_compatible

    try:
        server_version = client.info().version
    except Exception as exc:
        warnings.warn(
            f"Qdrant server version is unavailable ({exc.__class__.__name__}); "
            "client/server compatibility was not checked.",
            UserWarning,
            stacklevel=2,
        )
        return
    client_version = importlib.metadata.version("qdrant-client")
    if not is_compatible(client_version, server_version):
        warnings.warn(
            f"Qdrant client version {client_version} is incompatible with server version "
            f"{server_version}: major versions must match and minor versions may differ by at most 1.",
            UserWarning,
            stacklevel=2,
        )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
