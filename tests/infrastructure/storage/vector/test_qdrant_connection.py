from __future__ import annotations

import importlib.util
import re
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.storage.vector import connection
from infrastructure.storage.vector.connection import connect_qdrant, qdrant_bypasses_proxy


REPO_ROOT = Path(__file__).resolve().parents[4]


def _never_bypass(host: str) -> bool:
    return False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6333",
        "http://127.10.0.3:6333",
        "http://localhost:6333",
        "http://LOCALHOST:6333",
        "http://[::1]:6333",
    ],
)
def test_loopback_qdrant_connects_directly_even_when_the_platform_lists_no_bypass(url: str) -> None:
    assert qdrant_bypasses_proxy(url, bypass=_never_bypass) is True


def test_remote_qdrant_keeps_the_proxy_unless_the_platform_bypasses_its_host() -> None:
    seen: list[str] = []

    def bypass(host: str) -> bool:
        seen.append(host)
        return host == "qdrant.internal"

    assert qdrant_bypasses_proxy("https://qdrant.example.com:6333", bypass=bypass) is False
    assert qdrant_bypasses_proxy("http://qdrant.internal:6333", bypass=bypass) is True
    assert seen == ["qdrant.example.com", "qdrant.internal"]


def test_url_without_a_host_keeps_the_environment_settings() -> None:
    assert qdrant_bypasses_proxy(":memory:", bypass=lambda host: True) is False


def test_no_proxy_environment_is_honoured_for_remote_hosts(monkeypatch) -> None:
    monkeypatch.setenv("no_proxy", "vectors.corp.example")
    monkeypatch.setenv("NO_PROXY", "vectors.corp.example")

    assert qdrant_bypasses_proxy("http://vectors.corp.example:6333") is True


class _VersionedClient:
    def __init__(self, server_version: str | None = "1.17.1", error: Exception | None = None, **options: object) -> None:
        self.options = options
        self.server_version = server_version
        self.error = error

    def info(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(version=self.server_version)


def test_connect_qdrant_connects_bypassed_hosts_directly_and_checks_the_version_itself(monkeypatch) -> None:
    created: list[_VersionedClient] = []
    checked: list[_VersionedClient] = []

    def client_factory(**options: object) -> _VersionedClient:
        created.append(_VersionedClient(**options))
        return created[-1]

    monkeypatch.setattr("qdrant_client.QdrantClient", client_factory)
    monkeypatch.setattr(connection, "proxy_bypass", _never_bypass)
    monkeypatch.setattr(connection, "_warn_if_incompatible", checked.append)

    local = connect_qdrant("http://127.0.0.1:6333")
    remote = connect_qdrant("https://qdrant.example.com", api_key="secret", timeout=7)

    assert [client.options for client in created] == [
        {"url": "http://127.0.0.1:6333", "trust_env": False, "check_compatibility": False},
        {"url": "https://qdrant.example.com", "api_key": "secret", "timeout": 7},
    ]
    for thread in [t for t in threading.enumerate() if t.name == "qdrant-version-check"]:
        thread.join(timeout=5)
    assert checked == [local]
    assert remote not in checked


def test_version_check_is_silent_for_a_compatible_server(monkeypatch) -> None:
    monkeypatch.setattr(connection.importlib.metadata, "version", lambda name: "1.17.0")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        connection._warn_if_incompatible(_VersionedClient("1.17.1"))


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (_VersionedClient("0.9.0"), "incompatible with server version 0.9.0"),
        (_VersionedClient(error=ConnectionError("refused")), "server version is unavailable (ConnectionError)"),
    ],
)
def test_version_check_warns_on_incompatible_or_unreachable_server(monkeypatch, client, message) -> None:
    monkeypatch.setattr(connection.importlib.metadata, "version", lambda name: "1.17.0")

    with pytest.warns(UserWarning, match=re.escape(message)):
        connection._warn_if_incompatible(client)


def test_check_script_decides_proxy_bypass_like_the_connection_factory(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("check_qdrant", REPO_ROOT / "scripts" / "check_qdrant.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "proxy_bypass", lambda host: host == "qdrant.internal")

    for url in (
        "http://127.0.0.1:6333",
        "http://localhost:6333",
        "http://[::1]:6333",
        "http://qdrant.internal:6333",
        "https://qdrant.example.com",
        ":memory:",
    ):
        expected = qdrant_bypasses_proxy(url, bypass=lambda host: host == "qdrant.internal")
        assert script._bypasses_proxy(url) is expected, url
