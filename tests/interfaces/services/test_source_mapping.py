from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.foundation.models.source import (
    SourceDefinition,
    SourceFetchPolicy,
)
from backend.layers.signal.connector_tools import (
    register_arxiv_tools,
)
from backend.layers.signal.tools import (
    register_source_tools,
)
from framework.tool import ToolRegistry
from infrastructure.external.sources.models import (
    Lineage as InfraLineage,
    RawSourceItem as InfraRawSourceItem,
    SourceError as InfraSourceError,
    SourceType as InfraSourceType,
)
from interfaces.services.source_mapping import (
    to_business_fetch_policy,
    to_business_raw_source_item,
    to_business_source_error,
    to_infrastructure_fetch_policy,
    to_infrastructure_source_definition,
)
from interfaces.services.source_service import SourceFetchPreviewResult


@dataclass(frozen=True)
class _ArtifactRef:
    artifact_id: str

    def to_dict(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id}


def test_source_mapping_projects_definition_and_policy_without_shared_dto() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/feed.xml",
        reliability="high",
        authority_score=0.9,
        enabled=False,
        fetch_interval_seconds=7200,
        respect_robots=False,
        user_agent="NewsRoomContract/1.0",
        topics=["ai"],
        category="research",
        language="en",
        region="global",
        metadata={"priority": "p0"},
    )
    policy = SourceFetchPolicy(
        timeout_seconds=7.5,
        max_bytes=4096,
        max_redirects=4,
        user_agent="NewsRoomContract/1.0",
        respect_robots=False,
        rate_limit_per_domain_per_minute=12,
        retry_times=1,
        retry_on_status_codes=(429, 503),
        allowed_domains=("example.com",),
    )

    infra_source = to_infrastructure_source_definition(source)
    infra_policy = to_infrastructure_fetch_policy(policy)
    mapped_policy = to_business_fetch_policy(infra_policy)

    assert infra_source.source_id == source.source_id
    assert infra_source.name == source.name
    assert infra_source.source_type.value == "rss"
    assert infra_source.url == source.url
    assert infra_source.reliability.value == "high"
    assert infra_source.authority_score == 0.9
    assert infra_source.enabled is False
    assert infra_source.fetch_interval_seconds == 7200
    assert infra_source.respect_robots is False
    assert infra_source.user_agent == "NewsRoomContract/1.0"
    assert infra_source.topics == ["ai"]
    assert infra_source.category == "research"
    assert infra_source.language == "en"
    assert infra_source.region == "global"
    assert infra_source.metadata == {"priority": "p0"}
    assert infra_policy.timeout_seconds == 7.5
    assert infra_policy.max_bytes == 4096
    assert infra_policy.max_redirects == 4
    assert infra_policy.user_agent == "NewsRoomContract/1.0"
    assert infra_policy.respect_robots is False
    assert infra_policy.rate_limit_per_domain_per_minute == 12
    assert infra_policy.retry_times == 1
    assert infra_policy.retry_on_status_codes == (429, 503)
    assert mapped_policy.timeout_seconds == 7.5
    assert mapped_policy.max_bytes == 4096
    assert mapped_policy.max_redirects == 4
    assert mapped_policy.user_agent == "NewsRoomContract/1.0"
    assert mapped_policy.respect_robots is False
    assert mapped_policy.rate_limit_per_domain_per_minute == 12
    assert mapped_policy.retry_times == 1
    assert mapped_policy.retry_on_status_codes == (429, 503)

    # The egress guard enforces allowed_domains and allowed_private_networks on
    # every hop, so both directions of the mapping must carry them unchanged.
    business_policy_fields = {field.name for field in fields(policy)}
    infrastructure_policy_fields = {field.name for field in fields(infra_policy) if field.init}
    assert business_policy_fields == infrastructure_policy_fields
    assert infra_policy.allowed_domains == ("example.com",)
    assert mapped_policy.allowed_domains == ("example.com",)
    assert mapped_policy == policy


def test_source_mapping_projects_raw_items_without_losing_lineage() -> None:
    fetched_at = datetime(2026, 7, 19, 1, 30, tzinfo=timezone.utc)
    item = InfraRawSourceItem(
        source_item_id="raw-1",
        source_id="rss-source",
        source_name="RSS Source",
        source_type=InfraSourceType.RSS,
        title="Mapped item",
        url="https://example.com/item",
        fetched_at=fetched_at,
        raw_artifact_ref={"artifact_id": "raw-ref"},
        metadata={"nested": {"rank": 1}},
    )

    mapped = to_business_raw_source_item(item)

    assert mapped.source_item_id == "raw-1"
    assert mapped.fetched_at == fetched_at
    assert mapped.raw_artifact_ref == {"artifact_id": "raw-ref"}
    assert mapped.lineage is not None
    assert mapped.lineage.source_item_id == "raw-1"
    assert mapped.metadata == {"nested": {"rank": 1}}


def test_source_mapping_keeps_complete_public_raw_item_payload_stable() -> None:
    """All reachable Source projections must expose one complete payload.

    Business and infrastructure RawSourceItem models intentionally have
    different lifecycle ownership, but callers must not observe a different
    field set or timestamp representation depending on which DTO crossed the
    mapper boundary.
    """

    fetched_at = datetime(2026, 7, 19, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    published_at = datetime(2026, 7, 19, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    lineage = InfraLineage(
        source_id="rss-source",
        source_item_id="raw-1",
        normalized_item_id="normalized-1",
        ranked_item_id="ranked-1",
        raw_url="https://example.com/raw",
        canonical_url="https://example.com/canonical",
        fetched_at=fetched_at,
        published_at=published_at,
        raw_artifact_ref=_ArtifactRef("lineage-raw-ref"),
        parse_artifact_ref={"artifact_id": "lineage-parse-ref", "part": 2},
        metadata={"nested": {"lineage": ["kept", 1]}},
    )
    item = InfraRawSourceItem(
        source_item_id="raw-1",
        source_id="rss-source",
        source_name="RSS Source",
        source_type=InfraSourceType.RSS,
        title="Mapped item",
        url="https://example.com/item",
        fetched_at=fetched_at,
        published_at=published_at,
        summary="A complete summary",
        raw_content="The original content",
        raw_artifact_ref=_ArtifactRef("raw-ref"),
        parse_artifact_ref={"artifact_id": "parse-ref", "nested": {"attempt": 2}},
        authors=["Alice", "Bob"],
        tags=["ai", "research"],
        language="en",
        lineage=lineage,
        metadata={"nested": {"rank": [1, 2], "flags": {"trusted": True}}},
    )

    mapped = to_business_raw_source_item(item)

    def canonical_datetime(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    expected_lineage = {
        "source_id": "rss-source",
        "source_item_id": "raw-1",
        "normalized_item_id": "normalized-1",
        "ranked_item_id": "ranked-1",
        "raw_url": "https://example.com/raw",
        "canonical_url": "https://example.com/canonical",
        "fetched_at": canonical_datetime(fetched_at),
        "published_at": canonical_datetime(published_at),
        "raw_artifact_ref": {"artifact_id": "lineage-raw-ref"},
        "parse_artifact_ref": {"artifact_id": "lineage-parse-ref", "part": 2},
        "metadata": {"nested": {"lineage": ["kept", 1]}},
    }
    expected_payload = {
        "source_item_id": "raw-1",
        "source_id": "rss-source",
        "source_name": "RSS Source",
        "source_type": "rss",
        "title": "Mapped item",
        "url": "https://example.com/item",
        "fetched_at": canonical_datetime(fetched_at),
        "published_at": canonical_datetime(published_at),
        "summary": "A complete summary",
        "raw_content": "The original content",
        "raw_artifact_ref": {"artifact_id": "raw-ref"},
        "parse_artifact_ref": {"artifact_id": "parse-ref", "nested": {"attempt": 2}},
        "authors": ["Alice", "Bob"],
        "tags": ["ai", "research"],
        "language": "en",
        "lineage": expected_lineage,
        "metadata": {"nested": {"rank": [1, 2], "flags": {"trusted": True}}},
    }

    source_tool_registry = ToolRegistry()
    register_source_tools(
        source_tool_registry,
        source_runtime=SimpleNamespace(
            parse_feed=lambda *_args, **_kwargs: [mapped],
        ),
    )
    source_tool_result = source_tool_registry.require("source.parse_rss").executor(
        {
            "source": {
                "source_id": "rss-source",
                "name": "RSS Source",
                "source_type": "rss",
                "url": "https://example.com/feed.xml",
            },
            "xml": "<rss/>",
        }
    )

    connector_tool_registry = ToolRegistry()
    register_arxiv_tools(
        connector_tool_registry,
        connector=SimpleNamespace(
            fetch=lambda *_args, **_kwargs: ([item], []),
        ),
    )
    connector_tool_result = connector_tool_registry.require(
        "arxiv.search_papers"
    ).executor({"query": "cat:cs.AI", "limit": 1})

    interface_result = SourceFetchPreviewResult(
        source_id="rss-source",
        source_type="rss",
        query="ai",
        items=[item],
        errors=[],
    ).to_dict()

    public_payloads = {
        "infrastructure_model": item.to_dict(),
        "business_model": mapped.to_dict(),
        "interface_service": interface_result["items"][0],
        "source_tool": source_tool_result["items"][0],
        "connector_tool": connector_tool_result["items"][0],
    }

    for owner, payload in public_payloads.items():
        assert payload == expected_payload, owner


def test_source_mapping_round_trips_every_source_error_field_losslessly() -> None:
    occurred_at = datetime(2026, 7, 19, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    request_ref = _ArtifactRef("request-ref")
    response_ref = {"artifact_id": "response-ref", "attempt": 3}
    error = InfraSourceError(
        source_id="rss-source",
        source_name="RSS Source",
        error_type="fetch_http_503",
        error_message="upstream unavailable",
        url="https://example.com/feed.xml",
        retryable=False,
        request_ref=request_ref,
        response_ref=response_ref,
        occurred_at=occurred_at,
        metadata={
            "phase": "fetch",
            "retryable": False,
            "source_health_affecting": True,
            "workflow_blocking": False,
            "operator_action_required": False,
            "nested": {"attempts": [1, 2, {"backoff": 0.5}]},
        },
    )

    expected_payload = {
        "source_id": "rss-source",
        "source_name": "RSS Source",
        "error_type": "fetch_http_503",
        "error_message": "upstream unavailable",
        "url": "https://example.com/feed.xml",
        "retryable": False,
        "request_ref": {"artifact_id": "request-ref"},
        "response_ref": {"artifact_id": "response-ref", "attempt": 3},
        "occurred_at": occurred_at.isoformat(),
        "metadata": {
            "phase": "fetch",
            "retryable": False,
            "source_health_affecting": True,
            "workflow_blocking": False,
            "operator_action_required": False,
            "nested": {"attempts": [1, 2, {"backoff": 0.5}]},
        },
    }

    mapped = to_business_source_error(error)

    assert error.to_dict() == expected_payload
    assert mapped.to_dict() == expected_payload
    assert mapped.request_ref is request_ref
    assert mapped.response_ref is response_ref
    assert mapped.occurred_at is occurred_at
    assert mapped.occurred_at.utcoffset() == timedelta(hours=8)
