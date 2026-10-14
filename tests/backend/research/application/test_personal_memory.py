from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from backend.research.application.personal_memory import (
    GENERIC_READING_PREFERENCE_REASON,
    PERSONAL_MEMORY_EXPORT_SCHEMA,
    PersonalMemoryService,
    PersonalMemoryStateError,
)
from backend.research.domain.personal_memory import (
    MAX_QUERY_LENGTH,
    PersonalMemoryOwner,
    PersonalMemorySourceRef,
    PersonalMemoryStatus,
)
from backend.research.ports.personal_memory import (
    PersonalMemoryStoreConflictError,
    PersonalMemoryStoreNotFoundError,
    PersonalMemoryStoreReason,
)
from infrastructure.research.personal_memory_store import SQLitePersonalMemoryStore


class _Clock:
    def __init__(self) -> None:
        self._index = count()

    def __call__(self) -> datetime:
        return datetime(2026, 10, 14, 8, 0, tzinfo=UTC) + timedelta(
            seconds=next(self._index)
        )


@pytest.fixture
def owner() -> PersonalMemoryOwner:
    return PersonalMemoryOwner(
        user_id="user-a",
        tenant_id="tenant-a",
        namespace="research/personal",
    )


@pytest.fixture
def service(tmp_path):
    ids = (f"pmem_{index:032x}" for index in count(1))
    store = SQLitePersonalMemoryStore(tmp_path / "personal-memory.sqlite3")
    service = PersonalMemoryService(store, clock=_Clock(), id_factory=lambda: next(ids))
    yield service
    store.close()


def _source(ref: str = "paper://paper-1") -> list[PersonalMemorySourceRef]:
    return [
        PersonalMemorySourceRef(
            source_type="paper",
            source_ref=ref,
            source_revision="sha256:" + "a" * 64,
            locator="page:2",
        )
    ]


def test_candidate_requires_confirmation_before_recall_and_supports_cas_crud(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    candidate = service.create_candidate(
        owner,
        content="I am interested in graph neural network robustness.",
        category="research_interest",
        source_refs=_source(),
        client_request_id="candidate-1",
    )

    assert candidate.status is PersonalMemoryStatus.CANDIDATE
    assert (
        service.recall(owner, query="graph robustness", use_memory=True).records == ()
    )

    confirmed = service.confirm(
        owner,
        candidate.memory_id,
        expected_revision=1,
        content="Prioritize graph neural network robustness papers.",
    )
    assert confirmed.status is PersonalMemoryStatus.ACTIVE
    assert confirmed.revision == 2
    recalled = service.recall(owner, query="graph robustness", use_memory=True)
    assert recalled.used_memory_refs == (
        {"memory_id": confirmed.memory_id, "revision": 2},
    )
    assert recalled.records[0].record.content == confirmed.content
    assert recalled.records[0].reasons

    unchanged = service.update(
        owner,
        confirmed.memory_id,
        expected_revision=2,
        content=confirmed.content,
    )
    assert unchanged.revision == 2

    updated = service.update(
        owner,
        confirmed.memory_id,
        expected_revision=2,
        category="reading_preference",
    )
    assert updated.revision == 3
    with pytest.raises(PersonalMemoryStoreConflictError):
        service.update(
            owner,
            confirmed.memory_id,
            expected_revision=2,
            content="Stale overwrite",
        )

    disabled = service.disable(owner, confirmed.memory_id, expected_revision=3)
    assert disabled.status is PersonalMemoryStatus.DISABLED
    assert service.recall(owner, query="graph", use_memory=True).records == ()
    assert (
        service.disable(owner, confirmed.memory_id, expected_revision=4).revision == 4
    )

    enabled = service.enable(owner, confirmed.memory_id, expected_revision=4)
    assert enabled.status is PersonalMemoryStatus.ACTIVE
    assert service.recall(owner, query="graph", use_memory=True).records

    deleted = service.delete(owner, confirmed.memory_id, expected_revision=5)
    assert deleted.status is PersonalMemoryStatus.DELETED
    assert service.recall(owner, query="graph", use_memory=True).records == ()
    with pytest.raises(PersonalMemoryStateError):
        service.update(
            owner,
            confirmed.memory_id,
            expected_revision=6,
            content="Cannot revive",
        )


def test_manual_confirmed_path_ranking_pagination_export_and_local_opt_out(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    broad = service.create_confirmed(
        owner,
        content="Read machine learning papers with careful methods sections.",
        category="reading_preference",
        source_refs=_source("user://manual/broad"),
        client_request_id="manual-1",
    )
    exact = service.create_confirmed(
        owner,
        content="Machine learning reproducibility is the primary research interest.",
        category="research_interest",
        source_refs=_source("user://manual/exact"),
        client_request_id="manual-2",
    )

    result = service.recall(
        owner,
        query="machine learning reproducibility",
        limit=1,
        use_memory=True,
    )
    assert [item.record.memory_id for item in result.records] == [exact.memory_id]
    assert result.records[0].score > 0
    opted_out = service.recall(owner, query="machine learning", use_memory=False)
    assert opted_out.records == ()
    assert opted_out.used_memory_refs == ()
    assert opted_out.memory_enabled is False

    page = service.list(owner, limit=1)
    assert page.total == 2
    assert len(page.records) == 1
    assert page.next_offset == 1

    exported = service.export(owner).to_dict()
    assert exported["schema_version"] == PERSONAL_MEMORY_EXPORT_SCHEMA
    assert {record["memory_id"] for record in exported["records"]} == {
        broad.memory_id,
        exact.memory_id,
    }
    assert exported["owner"]["user_id"] == "user-a"
    assert exported["notices"]


def test_owner_is_never_taken_from_memory_payload_and_limits_are_enforced(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    record = service.create_confirmed(
        owner,
        content="Prefer concise summaries.",
        category="reading_preference",
        source_refs=[
            {
                "source_type": "user_input",
                "source_ref": "user://manual/preference",
                "label": "Settings",
            }
        ],
        client_request_id="manual-owner-boundary",
    )
    other = PersonalMemoryOwner(
        user_id="user-b",
        tenant_id="tenant-a",
        namespace=owner.namespace,
    )
    other_namespace = PersonalMemoryOwner(
        user_id=owner.user_id,
        tenant_id=owner.tenant_id,
        namespace="research/other",
    )

    with pytest.raises(PersonalMemoryStoreNotFoundError):
        service.get(other, record.memory_id)
    with pytest.raises(PersonalMemoryStoreNotFoundError):
        service.get(other_namespace, record.memory_id)
    with pytest.raises(ValueError, match="query"):
        service.recall(owner, query="x" * (MAX_QUERY_LENGTH + 1), use_memory=True)
    with pytest.raises(ValueError, match="limit"):
        service.list(owner, limit=101)
    with pytest.raises(TypeError, match="use_memory"):
        service.recall(owner, query="concise", use_memory=1)  # type: ignore[arg-type]


def test_candidate_rejection_creates_non_recallable_tombstone(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    candidate = service.create_candidate(
        owner,
        content="A model suggestion that the user rejects.",
        category="background",
        source_refs=_source("conversation://candidate-1"),
        client_request_id="candidate-reject-1",
    )

    rejected = service.reject_candidate(
        owner,
        candidate.memory_id,
        expected_revision=1,
    )

    assert rejected.status is PersonalMemoryStatus.DELETED
    assert rejected.revision == 2
    assert (
        service.recall(owner, query="model suggestion", use_memory=True).records == ()
    )
    with pytest.raises(PersonalMemoryStateError):
        service.confirm(owner, rejected.memory_id, expected_revision=2)


def test_remote_policy_defaults_off_uses_cas_and_does_not_disable_local_preview(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    record = service.create_confirmed(
        owner,
        content="Local preview remains available while remote processing is disabled.",
        category="reading_preference",
        source_refs=_source("user://policy/local-preview"),
        client_request_id="policy-local-preview",
    )

    default_policy = service.get_policy(owner)
    assert default_policy.allow_remote_processing is False
    assert default_policy.revision == 1
    assert service.recall(owner, query="local preview", use_memory=True).records

    enabled = service.update_policy(
        owner,
        allow_remote_processing=True,
        expected_revision=1,
    )
    assert enabled.allow_remote_processing is True
    assert enabled.revision == 2
    unchanged = service.update_policy(
        owner,
        allow_remote_processing=True,
        expected_revision=2,
    )
    assert unchanged.revision == 2
    with pytest.raises(PersonalMemoryStoreConflictError):
        service.update_policy(
            owner,
            allow_remote_processing=False,
            expected_revision=1,
        )

    exported = service.export(owner).to_dict()
    assert exported["policy"] == {
        "allow_remote_processing": True,
        "revision": 2,
        "created_at": default_policy.created_at.isoformat(),
        "updated_at": enabled.updated_at.isoformat(),
    }
    assert exported["records"][0]["memory_id"] == record.memory_id


def test_exact_active_selection_rejects_unknown_stale_and_inactive_records(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    first = service.create_confirmed(
        owner,
        content="First active snapshot memory.",
        category="background",
        source_refs=_source("user://snapshot/first"),
        client_request_id="snapshot-first",
    )
    second = service.create_confirmed(
        owner,
        content="Second active snapshot memory.",
        category="background",
        source_refs=_source("user://snapshot/second"),
        client_request_id="snapshot-second",
    )
    disabled = service.create_confirmed(
        owner,
        content="Disabled snapshot memory.",
        category="background",
        source_refs=_source("user://snapshot/disabled"),
        client_request_id="snapshot-disabled",
    )
    disabled = service.disable(owner, disabled.memory_id, expected_revision=1)
    deleted = service.create_confirmed(
        owner,
        content="Deleted snapshot memory.",
        category="background",
        source_refs=_source("user://snapshot/deleted"),
        client_request_id="snapshot-deleted",
    )
    deleted = service.delete(owner, deleted.memory_id, expected_revision=1)

    snapshot = service.select_active(
        owner,
        memory_refs=[
            {"memory_id": second.memory_id, "revision": second.revision},
            {"memory_id": first.memory_id, "revision": first.revision},
        ],
    )
    assert [record.memory_id for record in snapshot.records] == [
        second.memory_id,
        first.memory_id,
    ]
    assert snapshot.used_memory_refs == (
        {"memory_id": second.memory_id, "revision": 1},
        {"memory_id": first.memory_id, "revision": 1},
    )

    updated_first = service.update(
        owner,
        first.memory_id,
        expected_revision=1,
        content="Updated active snapshot memory.",
    )
    assert updated_first.revision == 2
    invalid_refs = (
        (
            {"memory_id": first.memory_id, "revision": 1},
            PersonalMemoryStoreReason.REVISION_CONFLICT,
        ),
        (
            {"memory_id": disabled.memory_id, "revision": disabled.revision},
            PersonalMemoryStoreReason.RECORD_NOT_ACTIVE,
        ),
        (
            {"memory_id": deleted.memory_id, "revision": deleted.revision},
            PersonalMemoryStoreReason.RECORD_NOT_ACTIVE,
        ),
        (
            {"memory_id": "pmem_" + "f" * 32, "revision": 1},
            PersonalMemoryStoreReason.NOT_FOUND,
        ),
    )
    for memory_ref, expected_reason in invalid_refs:
        with pytest.raises(PersonalMemoryStoreConflictError) as conflict:
            service.select_active(owner, memory_refs=[memory_ref])
        assert conflict.value.reason is expected_reason


def test_unrelated_query_recalls_confirmed_general_reading_preference_only(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    preference = service.create_confirmed(
        owner,
        content="解释用中文，先给结论。",
        category="reading_preference",
        source_refs=_source("user://preferences/answer-style"),
        client_request_id="generic-reading-preference",
    )
    unrelated_interest = service.create_confirmed(
        owner,
        content="I am interested in graph neural network robustness.",
        category="research_interest",
        source_refs=_source("user://interests/graph-robustness"),
        client_request_id="unrelated-research-interest",
    )

    result = service.recall(
        owner,
        query="How does contrastive decoding reduce hallucination?",
        use_memory=True,
    )

    assert [item.record.memory_id for item in result.records] == [preference.memory_id]
    assert unrelated_interest.memory_id not in {
        item.record.memory_id for item in result.records
    }
    assert result.records[0].score == 1
    assert result.records[0].reasons == (GENERIC_READING_PREFERENCE_REASON,)

    service.disable(owner, preference.memory_id, expected_revision=1)
    disabled_result = service.recall(
        owner,
        query="How does contrastive decoding reduce hallucination?",
        use_memory=True,
    )
    assert disabled_result.records == ()


def test_query_relevance_precedes_bounded_general_reading_preferences(
    service: PersonalMemoryService,
    owner: PersonalMemoryOwner,
) -> None:
    preferences = [
        service.create_confirmed(
            owner,
            content=content,
            category="reading_preference",
            source_refs=_source(f"user://preferences/{index}"),
            client_request_id=f"bounded-preference-{index}",
        )
        for index, content in enumerate(
            (
                "Explain in Chinese and lead with the conclusion.",
                "Use concise bullet points for methodology summaries.",
                "Call out evidence limitations explicitly.",
            ),
            start=1,
        )
    ]
    relevant_interest = service.create_confirmed(
        owner,
        content="Causal inference evaluation is a current research interest.",
        category="research_interest",
        source_refs=_source("user://interests/causal-inference"),
        client_request_id="relevant-causal-interest",
    )
    unrelated_interest = service.create_confirmed(
        owner,
        content="Graph compression is another research interest.",
        category="research_interest",
        source_refs=_source("user://interests/graph-compression"),
        client_request_id="unrelated-graph-interest",
    )

    result = service.recall(
        owner,
        query="causal inference evaluation",
        limit=2,
        use_memory=True,
    )

    assert len(result.records) == 2
    assert result.records[0].record.memory_id == relevant_interest.memory_id
    assert result.records[0].score > result.records[1].score
    assert result.records[1].record.memory_id in {
        preference.memory_id for preference in preferences
    }
    assert result.records[1].reasons == (GENERIC_READING_PREFERENCE_REASON,)
    assert unrelated_interest.memory_id not in {
        item.record.memory_id for item in result.records
    }
