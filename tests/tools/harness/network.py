"""Wire network tools to the production fetch path, pointed at local fault servers.

The egress guard refuses loopback by default, exactly as in production. Tests
that serve from a local fault server opt in with ``LOOPBACK`` the same way an
operator would allow an intranet range, so everything else about the guard
(redirect re-vetting, allowed_domains, credential stripping, the time budget)
stays in force.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.foundation.models.source import SourceFetchPolicy as BusinessFetchPolicy
from backend.layers.signal.source_health.manager import BasicSourceHealthManager
from infrastructure.external.sources.arxiv import ArxivConnector
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy as InfraFetchPolicy
from infrastructure.external.sources.github import GithubConnector
from interfaces.services.source_tool_runtime import default_source_tool_runtime
from tests.tools.harness.fault_stores import FaultySourceHealthStore
from tests.tools.harness.registry import ToolEnvironment

LOOPBACK = ("127.0.0.0/8", "::1/128")


def source_policy(**overrides: Any) -> BusinessFetchPolicy:
    options: dict[str, Any] = {
        "timeout_seconds": 2.0,
        "respect_robots": False,
        "retry_times": 0,
        "allowed_private_networks": LOOPBACK,
    }
    options.update(overrides)
    return BusinessFetchPolicy(**options)


def connector_policy(**overrides: Any) -> InfraFetchPolicy:
    options: dict[str, Any] = {
        "timeout_seconds": 2.0,
        "respect_robots": False,
        "retry_times": 0,
        "allowed_private_networks": LOOPBACK,
    }
    options.update(overrides)
    return InfraFetchPolicy(**options)


def source_tool_environment(
    build: Callable[..., ToolEnvironment],
    *,
    policy: BusinessFetchPolicy | None = None,
    health_store: FaultySourceHealthStore | None = None,
    **overrides: Any,
) -> ToolEnvironment:
    """Source tools over the real ``InfrastructureSourceToolRuntime`` and guarded opener."""

    options: dict[str, Any] = {
        "source_tool_runtime": default_source_tool_runtime(),
        "source_fetch_policy": policy or source_policy(),
    }
    if health_store is not None:
        options["source_health_manager"] = BasicSourceHealthManager(health_store=health_store)
    options.update(overrides)
    return build(**options)


def github_connector(api_base_url: str, **policy: Any) -> GithubConnector:
    return GithubConnector(fetch_policy=connector_policy(**policy), api_base_url=api_base_url)


def arxiv_connector(**policy: Any) -> ArxivConnector:
    return ArxivConnector(fetch_policy=connector_policy(**policy))


def source_payload(url: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"source_id": "fault-source", "name": "Fault source", "url": url}
    payload.update(fields)
    return payload
