from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.research.domain.personal_memory import (
    MAX_CONTENT_LENGTH,
    PersonalMemoryOwner,
    PersonalMemoryRecord,
    PersonalMemorySourceRef,
)


NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def _source() -> PersonalMemorySourceRef:
    return PersonalMemorySourceRef(
        source_type="paper",
        source_ref="paper://paper-1",
        source_revision="sha256:" + "a" * 64,
        locator="page:4",
        label="Original paper",
    )


def test_owner_and_record_serialization_keep_router_fields_stable() -> None:
    owner = PersonalMemoryOwner(
        user_id="user-a",
        tenant_id="tenant-a",
        namespace="research/personal",
    )
    record = PersonalMemoryRecord(
        memory_id="pmem_" + "1" * 32,
        owner=owner,
        content="Prefer papers with reproducible benchmark details.",
        category="reading_preference",
        source_refs=(_source(),),
        status="active",
        revision=3,
        created_at=NOW,
        updated_at=NOW,
        origin="user_confirmed",
        confirmed_at=NOW,
    )

    payload = record.to_dict()

    assert list(payload)[:8] == [
        "memory_id",
        "content",
        "category",
        "source_refs",
        "status",
        "revision",
        "created_at",
        "updated_at",
    ]
    assert payload["owner"] == {
        "user_id": "user-a",
        "tenant_id": "tenant-a",
        "namespace": "research/personal",
    }
    assert payload["source_refs"] == [
        {
            "source_type": "paper",
            "source_ref": "paper://paper-1",
            "source_revision": "sha256:" + "a" * 64,
            "locator": "page:4",
            "label": "Original paper",
        }
    ]


def test_candidate_and_deleted_lifecycle_are_explicit() -> None:
    owner = PersonalMemoryOwner(user_id="user-a", namespace="research/default")
    candidate = PersonalMemoryRecord(
        memory_id="pmem_" + "2" * 32,
        owner=owner,
        content="Candidate preference",
        category="research_interest",
        source_refs=(_source(),),
        status="candidate",
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        origin="model_candidate",
    )
    deleted = candidate.evolve(updated_at=NOW, status="deleted", deleted_at=NOW)

    assert candidate.recallable is False
    assert deleted.status.value == "deleted"
    assert deleted.revision == 2
    assert deleted.content == candidate.content

    with pytest.raises(ValueError, match="active memory requires confirmed_at"):
        PersonalMemoryRecord(
            memory_id="pmem_" + "3" * 32,
            owner=owner,
            content="Invalid active memory",
            category="background",
            source_refs=(_source(),),
            status="active",
            revision=1,
            created_at=NOW,
            updated_at=NOW,
            origin="user_confirmed",
        )


def test_domain_rejects_invalid_owner_content_sources_and_namespace() -> None:
    with pytest.raises(ValueError, match="namespace"):
        PersonalMemoryOwner(user_id="user-a", namespace="research/../foreign")
    with pytest.raises(ValueError, match="user_id"):
        PersonalMemoryOwner(user_id=" user-a ", namespace="research/default")
    with pytest.raises(ValueError, match="source_ref"):
        PersonalMemorySourceRef(source_type="paper", source_ref="")

    owner = PersonalMemoryOwner(user_id="user-a", namespace="research/default")
    with pytest.raises(ValueError, match="at most"):
        PersonalMemoryRecord(
            memory_id="pmem_" + "4" * 32,
            owner=owner,
            content="x" * (MAX_CONTENT_LENGTH + 1),
            category="background",
            source_refs=(_source(),),
            status="candidate",
            revision=1,
            created_at=NOW,
            updated_at=NOW,
            origin="model_candidate",
        )
    with pytest.raises(ValueError, match="at least one"):
        PersonalMemoryRecord(
            memory_id="pmem_" + "5" * 32,
            owner=owner,
            content="No provenance",
            category="background",
            source_refs=(),
            status="candidate",
            revision=1,
            created_at=NOW,
            updated_at=NOW,
            origin="model_candidate",
        )


def test_remote_policy_requires_boolean_and_monotonic_revision() -> None:
    from backend.research.domain.personal_memory import (
        PersonalMemoryRemoteChannel,
        PersonalMemoryRemotePolicy,
    )

    owner = PersonalMemoryOwner(user_id="user-a", namespace="research/default")
    policy = PersonalMemoryRemotePolicy(
        owner=owner,
        allow_remote_processing=False,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )

    assert policy.to_dict()["allow_remote_processing"] is False
    assert policy.to_dict()["remote_processing_permissions"] == {
        "model": False,
        "tool": False,
        "embedding": False,
    }
    assert all(
        not policy.allows_remote_processing(channel, use_memory=True)
        for channel in PersonalMemoryRemoteChannel
    )
    enabled = policy.evolve(allow_remote_processing=True, updated_at=NOW)
    assert enabled.revision == 2
    assert all(
        enabled.allows_remote_processing(channel, use_memory=True)
        for channel in PersonalMemoryRemoteChannel
    )
    assert all(
        not enabled.allows_remote_processing(channel, use_memory=False)
        for channel in PersonalMemoryRemoteChannel
    )
    with pytest.raises(TypeError, match="use_memory"):
        enabled.allows_remote_processing("model", use_memory=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="boolean"):
        PersonalMemoryRemotePolicy(
            owner=owner,
            allow_remote_processing=1,  # type: ignore[arg-type]
            revision=1,
            created_at=NOW,
            updated_at=NOW,
        )
