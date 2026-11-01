from __future__ import annotations

from backend.foundation.models.source import (
    RawSourceItem,
    SourceDefinition,
    SourceError,
    SourceFetchPolicy,
    SourceReliability,
    SourceType,
)
from infrastructure.external.sources.models import (
    RawSourceItem as InfraRawSourceItem,
    SourceDefinition as InfraSourceDefinition,
    SourceError as InfraSourceError,
    SourceReliability as InfraSourceReliability,
    SourceType as InfraSourceType,
)
from infrastructure.external.sources.fetch_policy import (
    SourceFetchPolicy as InfraSourceFetchPolicy,
)


def to_infrastructure_source_definition(
    source: SourceDefinition,
) -> InfraSourceDefinition:
    return InfraSourceDefinition(
        source_id=source.source_id,
        name=source.name,
        source_type=InfraSourceType(SourceType(source.source_type).value),
        url=source.url,
        reliability=InfraSourceReliability(SourceReliability(source.reliability).value),
        authority_score=source.authority_score,
        enabled=source.enabled,
        fetch_interval_seconds=source.fetch_interval_seconds,
        respect_robots=source.respect_robots,
        user_agent=source.user_agent,
        topics=list(source.topics),
        category=source.category,
        language=source.language,
        region=source.region,
        metadata=dict(source.metadata),
    )


def to_infrastructure_fetch_policy(
    policy: SourceFetchPolicy | InfraSourceFetchPolicy,
) -> InfraSourceFetchPolicy:
    if isinstance(policy, InfraSourceFetchPolicy):
        return policy
    return InfraSourceFetchPolicy(
        timeout_seconds=policy.timeout_seconds,
        max_bytes=policy.max_bytes,
        max_redirects=policy.max_redirects,
        user_agent=policy.user_agent,
        respect_robots=policy.respect_robots,
        rate_limit_per_domain_per_minute=policy.rate_limit_per_domain_per_minute,
        retry_times=policy.retry_times,
        retry_on_status_codes=tuple(policy.retry_on_status_codes),
        allowed_domains=tuple(policy.allowed_domains),
        allowed_private_networks=tuple(policy.allowed_private_networks),
    )


def to_business_fetch_policy(
    policy: SourceFetchPolicy | InfraSourceFetchPolicy,
) -> SourceFetchPolicy:
    if isinstance(policy, SourceFetchPolicy):
        return policy
    return SourceFetchPolicy(
        timeout_seconds=policy.timeout_seconds,
        max_bytes=policy.max_bytes,
        max_redirects=policy.max_redirects,
        user_agent=policy.user_agent,
        respect_robots=policy.respect_robots,
        rate_limit_per_domain_per_minute=policy.rate_limit_per_domain_per_minute,
        retry_times=policy.retry_times,
        retry_on_status_codes=tuple(policy.retry_on_status_codes),
        allowed_domains=tuple(policy.allowed_domains),
        allowed_private_networks=tuple(policy.allowed_private_networks),
    )


def to_business_raw_source_item(item: InfraRawSourceItem) -> RawSourceItem:
    return RawSourceItem(
        source_item_id=item.source_item_id,
        source_id=item.source_id,
        source_name=item.source_name,
        source_type=SourceType(item.source_type).value,
        title=item.title,
        url=item.url,
        fetched_at=item.fetched_at,
        published_at=item.published_at,
        summary=item.summary,
        raw_content=item.raw_content,
        raw_artifact_ref=item.raw_artifact_ref,
        parse_artifact_ref=item.parse_artifact_ref,
        authors=list(item.authors),
        tags=list(item.tags),
        language=item.language,
        lineage=item.lineage.to_dict() if item.lineage else None,
        metadata=dict(item.metadata),
    )


def to_business_source_error(error: InfraSourceError) -> SourceError:
    return SourceError(
        source_id=error.source_id,
        source_name=error.source_name,
        error_type=error.error_type,
        error_message=error.error_message,
        url=error.url,
        retryable=error.retryable,
        request_ref=error.request_ref,
        response_ref=error.response_ref,
        occurred_at=error.occurred_at,
        metadata=dict(error.metadata),
    )


__all__ = [
    "to_business_fetch_policy",
    "to_business_raw_source_item",
    "to_business_source_error",
    "to_infrastructure_fetch_policy",
    "to_infrastructure_source_definition",
]
