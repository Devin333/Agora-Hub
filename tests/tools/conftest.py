from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.tools.harness.fault_http import FaultHTTPServer
from tests.tools.harness.fault_smtp import FaultSMTPServer, SMTPScript
from tests.tools.harness.leaks import DEFAULT_GRACE_SECONDS, assert_no_leaks, snapshot_resources
from tests.tools.harness.registry import ToolEnvironment, build_tool_environment
from tests.tools.harness.tls import LoopbackTLS, create_loopback_tls


@pytest.fixture(autouse=True)
def resource_leak_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail any tool test that leaves a thread or attempt slot behind.

    Autouse fixtures are set up first and torn down last, so every fault
    server below is already stopped and released when this check runs.
    """

    marker = request.node.get_closest_marker("leak_grace")
    grace = float(marker.args[0]) if marker is not None else DEFAULT_GRACE_SECONDS
    baseline = snapshot_resources()
    yield
    assert_no_leaks(baseline, grace_seconds=grace)


@pytest.fixture
def fault_http() -> Iterator[FaultHTTPServer]:
    with FaultHTTPServer() as server:
        yield server


@pytest.fixture(scope="session")
def loopback_tls(tmp_path_factory: pytest.TempPathFactory) -> LoopbackTLS:
    return create_loopback_tls(tmp_path_factory.mktemp("loopback-tls"))


@pytest.fixture
def fault_https(loopback_tls: LoopbackTLS) -> Iterator[FaultHTTPServer]:
    with FaultHTTPServer(ssl_context=loopback_tls.server_context()) as server:
        yield server


@pytest.fixture
def fault_smtp() -> Iterator[Callable[[SMTPScript], FaultSMTPServer]]:
    started: list[FaultSMTPServer] = []

    def start(script: SMTPScript) -> FaultSMTPServer:
        server = FaultSMTPServer(script).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


@pytest.fixture
def tool_env_factory(tmp_path: Path) -> Iterator[Callable[..., ToolEnvironment]]:
    environments: list[ToolEnvironment] = []

    def build(**overrides: object) -> ToolEnvironment:
        environment = build_tool_environment(tmp_path / f"env-{len(environments)}", **overrides)
        environments.append(environment)
        return environment

    yield build
    for environment in environments:
        environment.release()


@pytest.fixture
def tool_env(tool_env_factory: Callable[..., ToolEnvironment]) -> ToolEnvironment:
    return tool_env_factory()
