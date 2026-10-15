from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.research.application.personal_memory import PersonalMemoryService
from backend.research.domain.personal_memory import PersonalMemoryOwner, PersonalMemorySelection
from infrastructure.research.personal_memory_store import SQLitePersonalMemoryStore
from interfaces.api.routers.research_desktop import DesktopAskRequest
from interfaces.services.research_desktop_control import (
    LocalJsonResearchDesktopRequestStore,
    ResearchDesktopControlError,
    canonical_request_fingerprint,
)
from interfaces.services.research_desktop_service import (
    ResearchDesktopApplicationService,
    ResearchDesktopOwner,
    ResearchDesktopServiceError,
    _PersonalMemoryTaskContextProvider,
)
from interfaces.services.research_import_service import ResearchImportService
from interfaces.services.research_service import ResearchApplicationService


SOURCE_REVISION = "sha256:" + "a" * 64
SOURCE_FINGERPRINT = SOURCE_REVISION


class _ResearchFacade(ResearchApplicationService):
    def __init__(self) -> None:
        self.ask_calls = []
        self.contexts = []
        self.used_personal_memory_override = None

    def capability_runtime_identity(self):
        return "sha256:" + "f" * 64

    def capability_projection(self):
        return {
            "catalog": True,
            "parse": True,
            "analyze": True,
            "ask": True,
            "ragAsk": True,
            "artifactRead": True,
        }

    def get_run_projection(self, run_id, *, actor):
        return {
            "runId": run_id,
            "paperId": "paper-1",
            "status": "succeeded",
            "accepted": True,
            "qualityPassed": True,
            "eligibleForAcceptedExport": True,
            "artifactStatus": "valid",
            "sourceRevision": SOURCE_REVISION,
            "sourceFingerprint": SOURCE_FINGERPRINT,
        }

    def ask_paper(self, paper_id, request):
        self.ask_calls.append((paper_id, request))
        context = (
            request.personal_memory_context.current()
            if request.personal_memory_context is not None
            else None
        )
        self.contexts.append(context)
        return {
            "answer": "Grounded answer",
            "abstained": False,
            "citations": [
                {
                    "id": "citation:1",
                    "label": "Methods",
                    "evidenceId": "evidence:methods:1",
                    "paperId": paper_id,
                    "sourceRevision": SOURCE_REVISION,
                    "valid": True,
                    "sectionId": "methods",
                }
            ],
            "evidenceRefs": ["evidence:methods:1"],
            "decision": {"decision_type": "return_answer"},
            "claims": [],
            "confidence": 1.0,
            "transcriptId": "transcript-1",
            "status": "answered",
            "runId": request.run_id,
            "paperId": paper_id,
            "metadata": {
                "retrievalGoalId": "goal-1",
                "generationMode": "gated_harness",
                "recovered": False,
            },
            **(
                {"usedPersonalMemory": self.used_personal_memory_override}
                if self.used_personal_memory_override is not None
                else (
                    {"usedPersonalMemory": list(context.used_refs)}
                    if context is not None
                    else {}
                )
            ),
        }

    def recover_paper_answer(self, paper_id, request, *, include_interrupted=False):
        return None


@pytest.fixture
def memory_service(tmp_path: Path):
    store = SQLitePersonalMemoryStore(tmp_path / "personal-memory.sqlite3")
    service = PersonalMemoryService(store)
    yield service
    store.close()


def _desktop_owner(user_id: str = "alice") -> ResearchDesktopOwner:
    return ResearchDesktopOwner(user_id=user_id, tenant_id="tenant-a")


def _memory_owner(user_id: str = "alice") -> PersonalMemoryOwner:
    return PersonalMemoryOwner(
        user_id=user_id,
        tenant_id="tenant-a",
        namespace=f"research:user:{user_id}",
    )


def _confirmed(
    service: PersonalMemoryService,
    *,
    user_id: str = "alice",
    request_id: str = "memory-1",
    content: str = "Prefer methods-first explanations.",
):
    return service.create_confirmed(
        _memory_owner(user_id),
        content=content,
        category="reading_preference",
        source_refs=[
            {
                "source_type": "user_input",
                "source_ref": f"user://{user_id}/{request_id}",
            }
        ],
        client_request_id=request_id,
    )


def _enable_remote(service: PersonalMemoryService, user_id: str = "alice"):
    owner = _memory_owner(user_id)
    policy = service.get_policy(owner)
    return service.update_policy(
        owner,
        allow_remote_processing=True,
        expected_revision=policy.revision,
    )


def _service(tmp_path: Path, memory: PersonalMemoryService):
    facade = _ResearchFacade()
    service = ResearchDesktopApplicationService(
        research=facade,
        requests=LocalJsonResearchDesktopRequestStore(tmp_path / "control.json"),
        imports=ResearchImportService(tmp_path / "imports"),
        personal_memory=memory,
    )
    return service, facade


def _semantic(record, policy_revision: int):
    return {
        "kind": "ask",
        "runId": "run-1",
        "paperId": "paper-1",
        "sourceRevision": SOURCE_REVISION,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "question": "What is the method?",
        "selection": {},
        "locale": "en",
        "personalMemory": {
            "refs": [{"memoryId": record.memory_id, "revision": record.revision}],
            "policyRevision": policy_revision,
        },
    }


def _submit(service, owner, semantic, *, request_id: str):
    return service.submit_ask(
        owner,
        request_id=request_id,
        generation=service.generation(),
        request_fingerprint=canonical_request_fingerprint(semantic),
        run_id=semantic["runId"],
        paper_id=semantic["paperId"],
        source_revision=semantic["sourceRevision"],
        source_fingerprint=semantic["sourceFingerprint"],
        question=semantic["question"],
        selection=semantic["selection"],
        locale=semantic["locale"],
        personal_memory=semantic.get("personalMemory"),
    )


def test_desktop_request_schema_accepts_only_exact_refs_without_owner_or_content() -> None:
    base = {
        "requestId": "ask-schema-0001",
        "generation": "gen_" + "1" * 32,
        "requestFingerprint": "sha256:" + "2" * 64,
        "runId": "run-1",
        "paperId": "paper-1",
        "sourceRevision": SOURCE_REVISION,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "question": "What is the method?",
        "selection": {},
        "locale": "en",
        "personalMemory": {
            "refs": [{"memoryId": "pmem_" + "3" * 32, "revision": 1}],
            "policyRevision": 2,
        },
    }

    assert DesktopAskRequest.model_validate(base).personalMemory is not None
    for forged in (
        {**base, "personalMemory": None},
        {
            **base,
            "personalMemory": {**base["personalMemory"], "owner": "forged"},
        },
        {
            **base,
            "personalMemory": {
                **base["personalMemory"],
                "refs": [{
                    **base["personalMemory"]["refs"][0],
                    "content": "forged",
                }],
            },
        },
    ):
        with pytest.raises(ValidationError):
            DesktopAskRequest.model_validate(forged)


def test_exact_owner_policy_and_active_revision_reach_real_task_context(
    tmp_path: Path,
    memory_service: PersonalMemoryService,
) -> None:
    record = _confirmed(memory_service)
    policy = _enable_remote(memory_service)
    desktop, research = _service(tmp_path, memory_service)

    result = _submit(
        desktop,
        _desktop_owner(),
        _semantic(record, policy.revision),
        request_id="ask-memory-0001",
    )

    assert research.contexts[0].items[0].content == record.content
    assert result["result"]["usedPersonalMemory"] == [
        {"memoryId": record.memory_id, "revision": record.revision}
    ]
    assert research.ask_calls[0][1].question == "What is the method?"


def test_omitted_selection_sends_no_memory_and_discloses_no_refs(
    tmp_path: Path,
    memory_service: PersonalMemoryService,
) -> None:
    desktop, research = _service(tmp_path, memory_service)
    semantic = {
        "kind": "ask",
        "runId": "run-1",
        "paperId": "paper-1",
        "sourceRevision": SOURCE_REVISION,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "question": "What is the method?",
        "selection": {},
        "locale": "en",
    }

    result = _submit(
        desktop,
        _desktop_owner(),
        semantic,
        request_id="ask-memory-0002",
    )

    assert research.contexts == [None]
    assert "usedPersonalMemory" not in result


def test_disabled_remote_policy_refuses_before_worker_call(
    tmp_path: Path,
    memory_service: PersonalMemoryService,
) -> None:
    record = _confirmed(memory_service)
    policy = memory_service.get_policy(_memory_owner())
    desktop, research = _service(tmp_path, memory_service)

    with pytest.raises(ResearchDesktopServiceError) as raised:
        _submit(
            desktop,
            _desktop_owner(),
            _semantic(record, policy.revision),
            request_id="ask-memory-0003",
        )

    assert raised.value.code == "personal_memory_remote_processing_disabled"
    assert raised.value.status_code == 403
    assert research.ask_calls == []


def test_policy_revision_change_refuses_without_silent_upgrade(
    tmp_path: Path,
    memory_service: PersonalMemoryService,
) -> None:
    record = _confirmed(memory_service)
    old_policy = memory_service.get_policy(_memory_owner())
    _enable_remote(memory_service)
    desktop, research = _service(tmp_path, memory_service)

    with pytest.raises(ResearchDesktopServiceError) as raised:
        _submit(
            desktop,
            _desktop_owner(),
            _semantic(record, old_policy.revision),
            request_id="ask-memory-0004",
        )

    assert raised.value.code == "personal_memory_policy_conflict"
    assert raised.value.status_code == 409
    assert research.ask_calls == []


@pytest.mark.parametrize("state", ["stale", "disabled", "deleted", "wrong-owner"])
def test_invalid_exact_selection_is_a_controlled_conflict_before_worker(
    tmp_path: Path,
    memory_service: PersonalMemoryService,
    state: str,
) -> None:
    record = _confirmed(memory_service)
    policy = _enable_remote(memory_service)
    if state == "stale":
        memory_service.update(
            _memory_owner(),
            record.memory_id,
            expected_revision=record.revision,
            content="Prefer concise methods-first explanations.",
        )
    elif state == "disabled":
        memory_service.disable(
            _memory_owner(), record.memory_id, expected_revision=record.revision
        )
    elif state == "deleted":
        memory_service.delete(
            _memory_owner(), record.memory_id, expected_revision=record.revision
        )
    else:
        record = _confirmed(
            memory_service,
            user_id="bob",
            request_id="memory-bob",
        )
    desktop, research = _service(tmp_path, memory_service)

    with pytest.raises(ResearchDesktopServiceError) as raised:
        _submit(
            desktop,
            _desktop_owner(),
            _semantic(record, policy.revision),
            request_id=f"ask-memory-{state}",
        )

    assert raised.value.code == "personal_memory_selection_conflict"
    assert raised.value.status_code == 409
    assert research.ask_calls == []


def test_context_provider_rechecks_policy_after_loading_records(
    memory_service: PersonalMemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _confirmed(memory_service)
    policy = _enable_remote(memory_service)
    original_select = memory_service.select_active

    def select_then_disable(owner, *, memory_refs):
        snapshot = original_select(owner, memory_refs=memory_refs)
        memory_service.update_policy(
            owner,
            allow_remote_processing=False,
            expected_revision=policy.revision,
        )
        return snapshot

    monkeypatch.setattr(memory_service, "select_active", select_then_disable)
    provider = _PersonalMemoryTaskContextProvider(
        application=memory_service,
        owner=_memory_owner(),
        policy_revision=policy.revision,
        selections=(
            PersonalMemorySelection(memory_id=record.memory_id, revision=record.revision),
        ),
    )

    with pytest.raises(ResearchDesktopServiceError) as raised:
        provider.current()

    assert raised.value.code == "personal_memory_policy_conflict"


def test_malformed_worker_memory_receipt_is_not_silently_removed_by_projection(
    tmp_path: Path,
    memory_service: PersonalMemoryService,
) -> None:
    record = _confirmed(memory_service)
    policy = _enable_remote(memory_service)
    service, facade = _service(tmp_path, memory_service)
    semantic = _semantic(record, policy.revision)
    facade.used_personal_memory_override = [
        {
            "memoryId": record.memory_id,
            "revision": record.revision,
            "content": "must-not-be-disclosed",
        }
    ]

    response = _submit(service, _desktop_owner(), semantic, request_id="ask-malformed-receipt")

    assert response["status"] == "needs_action"
    assert response["errorCode"] == "personal_memory_disclosure_mismatch"
    assert len(facade.ask_calls) == 1


def test_memory_selection_is_part_of_idempotent_request_identity(
    tmp_path: Path,
    memory_service: PersonalMemoryService,
) -> None:
    first = _confirmed(memory_service, request_id="memory-first")
    second = _confirmed(memory_service, request_id="memory-second")
    policy = _enable_remote(memory_service)
    desktop, research = _service(tmp_path, memory_service)
    request_id = "ask-memory-identity"

    _submit(
        desktop,
        _desktop_owner(),
        _semantic(first, policy.revision),
        request_id=request_id,
    )
    with pytest.raises(ResearchDesktopControlError) as raised:
        _submit(
            desktop,
            _desktop_owner(),
            _semantic(second, policy.revision),
            request_id=request_id,
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "desktop_request_conflict"
    assert len(research.ask_calls) == 1


def _strict_grounded_answer():
    return {
        "runId": "run-1", "paperId": "paper-1", "answer": "Verified [1].",
        "status": "answered", "abstained": False, "confidence": 1.0,
        "decision": {"decision_type": "return_answer"},
        "transcriptId": "rag-transcript://session-real/transcript-real",
        "evidenceRefs": ["ev-real"],
        "citations": [{"id": "cite-real", "label": "Methods", "evidenceId": "ev-real",
                       "paperId": "paper-1", "sourceRevision": SOURCE_REVISION,
                       "sectionId": "methods", "valid": True}],
        "claims": ["not public"], "metadata": {"private": "not public"},
    }


def test_desktop_grounded_answer_projects_only_strict_trace_contract() -> None:
    from interfaces.services.research_desktop_service import (
        _ask_operation_result, _ask_result_has_verified_citations,
    )

    raw = _strict_grounded_answer()
    assert _ask_result_has_verified_citations(raw, paper_id="paper-1", source_revision=SOURCE_REVISION)
    projected = _ask_operation_result(raw, source_revision=SOURCE_REVISION)
    assert projected["evidenceRefs"] == ["ev-real"]
    assert projected["transcriptId"] == raw["transcriptId"]
    assert projected["answerStatus"] == "answered"
    assert "status" not in projected and "abstained" not in projected
    assert "claims" not in projected and "metadata" not in projected and "decision" not in projected
    assert "answerStatus" in projected


@pytest.mark.parametrize("field,bad", [
    ("evidenceRefs", None), ("evidenceRefs", ["cite-real"]),
    ("evidenceRefs", ["ev-real", "ev-real"]), ("evidenceRefs", ["other"]),
    ("transcriptId", None), ("transcriptId", ""), ("transcriptId", "x" * 257),
    ("transcriptId", "rag-transcript://with space"), ("transcriptId", "rag-transcript://bad\x00"),
    ("status", "succeeded"), ("status", "completed"), ("abstained", True),
    ("abstained", None), ("decision", {"decision_type": "abstain"}),
])
def test_desktop_grounded_answer_requires_exact_authoritative_trace(field, bad) -> None:
    from interfaces.services.research_desktop_service import _ask_result_has_verified_citations

    raw = _strict_grounded_answer()
    raw[field] = bad
    assert not _ask_result_has_verified_citations(raw, paper_id="paper-1", source_revision=SOURCE_REVISION)


def test_locator_rejection_keeps_real_trace_for_persisted_request_audit() -> None:
    from interfaces.services.research_desktop_service import (
        _answer_rejection_code, _answer_trace_for_audit,
    )

    raw = _strict_grounded_answer()
    raw["answer"] = None
    raw["abstained"] = True
    raw["metadata"] = {"citationLocatorVerified": False}
    assert _answer_rejection_code(raw) == "answer_citation_locator_unverified"
    trace = _answer_trace_for_audit(raw)
    assert trace == {
        "answerStatus": "answered", "transcriptId": raw["transcriptId"],
        "evidenceRefs": ["ev-real"], "decision": {"decision_type": "return_answer"},
    }
    assert "answer" not in trace


def test_rejected_query_status_projects_only_the_persisted_answer_trace() -> None:
    from types import SimpleNamespace
    from interfaces.services.research_desktop_service import ResearchDesktopApplicationService

    admission = SimpleNamespace(to_dict=lambda: {
        "requestId": "request-real", "generation": "gen_" + "1" * 32,
        "fingerprint": "sha256:" + "2" * 64, "kind": "ask", "status": "failed",
        "runId": "run-1", "semantic": {"paperId": "paper-1"},
        "error": {"code": "answer_citation_locator_unverified", "answerTrace": {
            "answerStatus": "answered", "transcriptId": "rag-transcript://real",
            "evidenceRefs": ["ev-real"], "decision": {"decision_type": "return_answer"},
        }},
    })
    operation = ResearchDesktopApplicationService._operation_response(admission)
    assert operation["status"] == "rejected"
    assert operation["result"]["transcriptId"] == "rag-transcript://real"
    assert operation["result"]["evidenceRefs"] == ["ev-real"]
    assert "answer" not in operation["result"]
