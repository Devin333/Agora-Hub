from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore
from interfaces.services.research_desktop_control import (
    LocalJsonResearchDesktopRequestStore,
    ResearchDesktopControlError,
    canonical_request_fingerprint,
)
from interfaces.services.research_desktop_service import (
    ResearchDesktopApplicationService,
    ResearchDesktopOwner,
    ResearchDesktopServiceError,
)
from interfaces.services.research_import_service import ResearchImportService
from tests.interfaces.services.test_research_desktop_operations import _ResearchFacade


OWNER = ResearchDesktopOwner(user_id="alice")
OTHER_OWNER = ResearchDesktopOwner(user_id="bob")
REVISION = "sha256:" + "a" * 64


def _service(root: Path, research: _ResearchFacade | None = None) -> tuple[ResearchDesktopApplicationService, _ResearchFacade, LocalJsonResearchDesktopRequestStore]:
    facade = research or _ResearchFacade()
    store = LocalJsonResearchDesktopRequestStore(root / "control.json")
    return (
        ResearchDesktopApplicationService(
            research=facade,
            requests=store,
            imports=ResearchImportService(root / "imports"),
        ),
        facade,
        store,
    )


def _prepare_semantic(source_url: str = "https://example.test/paper.pdf") -> dict[str, object]:
    return {"kind": "prepare", "paperId": "paper-1", "sourceUrl": source_url, "sourceType": None}


def _analysis_semantic(*, revision: str = REVISION) -> dict[str, object]:
    return {
        "kind": "analyze",
        "paperId": "paper-1",
        "sourceRevision": revision,
        "sourceFingerprint": revision,
        "sourceUrl": "https://example.test/paper.pdf",
        "pdfUrl": None,
        "objective": None,
        "constraints": ["preserve deterministic evidence gates"],
    }


class _DropFirstTerminalWriteStore(LocalJsonResearchDesktopRequestStore):
    def __init__(self, path: Path, *, request_ids: set[str]) -> None:
        super().__init__(path)
        self._request_ids = set(request_ids)

    def complete(self, **kwargs):
        request_id = str(kwargs.get("request_id") or "")
        if request_id in self._request_ids:
            self._request_ids.remove(request_id)
            raise OSError("simulated terminal receipt write interruption")
        return super().complete(**kwargs)


def test_business_completion_is_reconciled_after_terminal_receipt_write_interruption_and_identity_is_fenced(tmp_path: Path) -> None:
    research = _ResearchFacade()
    store = _DropFirstTerminalWriteStore(
        tmp_path / "control.json",
        request_ids={"prepare-contract-1", "analyze-contract-1"},
    )
    imports = ResearchImportService(tmp_path / "imports")
    service = ResearchDesktopApplicationService(research=research, requests=store, imports=imports)
    generation = service.generation()
    prepare = _prepare_semantic()
    analysis = _analysis_semantic()

    with pytest.raises(OSError, match="terminal receipt write interruption"):
        service.submit_prepare(
            OWNER,
            request_id="prepare-contract-1",
            generation=generation,
            request_fingerprint=canonical_request_fingerprint(prepare),
            paper_id="paper-1",
            source_url=str(prepare["sourceUrl"]),
            source_type=None,
        )
    assert len(research.parse_calls) == 1

    rebuilt = ResearchDesktopApplicationService(
        research=research,
        requests=LocalJsonResearchDesktopRequestStore(store.path),
        imports=imports,
    )
    recovered_prepare = rebuilt.get_operation(
        OWNER, request_id="prepare-contract-1", generation=generation
    )
    assert recovered_prepare["status"] == "passed"
    assert recovered_prepare["result"]["sourceRevision"] == REVISION
    assert len(research.parse_calls) == 1

    interrupted = ResearchDesktopApplicationService(
        research=research,
        requests=store,
        imports=imports,
    )
    with pytest.raises(OSError, match="terminal receipt write interruption"):
        interrupted.submit_analysis(
            OWNER,
            request_id="analyze-contract-1",
            generation=generation,
            request_fingerprint=canonical_request_fingerprint(analysis),
            paper_id="paper-1",
            source_revision=REVISION,
            source_fingerprint=REVISION,
            source_url=str(analysis["sourceUrl"]),
            objective=None,
            constraints=analysis["constraints"],
        )
    assert len(research.analysis_calls) == 1

    rebuilt_again = ResearchDesktopApplicationService(
        research=research,
        requests=LocalJsonResearchDesktopRequestStore(store.path),
        imports=imports,
    )
    recovered_analysis = rebuilt_again.get_operation(
        OWNER, request_id="analyze-contract-1", generation=generation
    )
    assert recovered_analysis["status"] == "passed"
    assert recovered_analysis["result"]["sourceRevision"] == REVISION
    assert len(research.analysis_calls) == 1

    with pytest.raises(ResearchDesktopControlError) as changed:
        rebuilt_again.submit_prepare(
            OWNER,
            request_id="prepare-contract-1",
            generation=generation,
            request_fingerprint=canonical_request_fingerprint(
                _prepare_semantic("https://example.test/other.pdf")
            ),
            paper_id="paper-1",
            source_url="https://example.test/other.pdf",
            source_type=None,
        )
    assert changed.value.code == "desktop_request_conflict"
    with pytest.raises(ResearchDesktopServiceError) as foreign:
        rebuilt_again.get_operation(
            OTHER_OWNER, request_id="prepare-contract-1", generation=generation
        )
    assert foreign.value.code == "desktop_request_not_found"

    # A request carrying another control state's generation is fenced.
    foreign_generation = "gen_" + "f" * 32
    assert foreign_generation != generation
    with pytest.raises(ResearchDesktopControlError) as stale:
        rebuilt_again.get_operation(
            OWNER, request_id="prepare-contract-1", generation=foreign_generation
        )
    assert stale.value.code == "desktop_generation_stale"

class _PrivateImportResearch(_ResearchFacade):
    def __init__(self, imports: ResearchImportService, *, user_id: str, import_id: str, paper_id: str) -> None:
        super().__init__()
        self.imports = imports
        self.user_id = user_id
        self.import_id = import_id
        self.paper_id = paper_id

    def resolve_analyze_source(self, paper_id, *, source_revision, source_fingerprint, actor):
        assert paper_id == self.paper_id
        assert actor.user_id == self.user_id
        owned = self.imports.resolve_completed_source(
            user_id=self.user_id,
            paper_id=paper_id,
            source_revision=source_revision,
            source_fingerprint=source_fingerprint,
        )
        return owned["path"].resolve().as_uri()


def test_private_pdf_analysis_uses_owned_bytes_and_rejects_tampering_or_caller_attestation(tmp_path: Path) -> None:
    imports = ResearchImportService(tmp_path / "imports")
    content = b"%PDF-1.7\nprivate-owned-source"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    received = imports.create_received(
        user_id="alice",
        content=content,
        filename="private.pdf",
        source_revision=digest,
        source_fingerprint=digest,
    )
    imports.mark_completed(user_id="alice", import_id=received["importId"], result={"paperId": "paper-private"})
    research = _PrivateImportResearch(imports, user_id="alice", import_id=received["importId"], paper_id="paper-private")
    research.identity = {"paperId": "paper-private", "sourceRevision": digest, "sourceFingerprint": digest}
    store = LocalJsonResearchDesktopRequestStore(tmp_path / "control.json")
    service = ResearchDesktopApplicationService(research=research, requests=store, imports=imports)
    generation = service.generation()
    semantic = {
        "kind": "analyze", "paperId": "paper-private", "sourceRevision": digest,
        "sourceFingerprint": digest, "sourceUrl": None, "pdfUrl": None,
        "objective": None, "constraints": ["private owner bytes only"],
    }

    completed = service.submit_analysis(
        OWNER,
        request_id="private-analysis-1",
        generation=generation,
        request_fingerprint=canonical_request_fingerprint(semantic),
        paper_id="paper-private",
        source_revision=digest,
        source_fingerprint=digest,
        objective=None,
        constraints=semantic["constraints"],
    )
    assert completed["status"] == "passed"
    assert completed["result"]["paperId"] == "paper-private"
    assert completed["result"]["sourceRevision"] == digest
    assert research.analysis_calls[0].source_url is None
    assert research.analysis_calls[0].pdf_url.startswith("file:")

    owned_path = imports.source_path(user_id="alice", import_id=received["importId"])
    owned_path.write_bytes(b"%PDF-1.7\ntampered")
    tampered_semantic = {**semantic, "objective": "caller says checksum is valid"}
    with pytest.raises(ResearchDesktopServiceError) as tampered:
        service.submit_analysis(
            OWNER,
            request_id="private-analysis-2",
            generation=generation,
            request_fingerprint=canonical_request_fingerprint(tampered_semantic),
            paper_id="paper-private",
            source_revision=digest,
            source_fingerprint=digest,
            objective=str(tampered_semantic["objective"]),
            constraints=semantic["constraints"],
        )
    assert tampered.value.code == "source_identity_unavailable"
    assert len(research.analysis_calls) == 1


class _JournalResearch(_ResearchFacade):
    def __init__(self, transcripts: PaperRagTranscriptFileStore) -> None:
        super().__init__()
        self.transcripts = transcripts
        self.ask_execution_count = 0
        self.response: dict[str, object] = {}

    def ask_paper(self, paper_id, request):
        self.ask_execution_count += 1
        return dict(self.response)

    def recover_paper_answer(self, paper_id, request, *, include_interrupted=False):
        del paper_id, include_interrupted
        try:
            journal = self.transcripts.load_journal(
                request.request_id,
                actor_scope={"user_id": request.user_id, "memory_namespace": request.memory_namespace},
            )
        except (OSError, ValueError, PermissionError):
            return {"status": "interrupted", "recoveryState": "needs_action", "errorCode": "rag_journal_invalid", "runId": request.run_id, "paperId": "paper-1"}
        return None if journal is None else {"status": "interrupted", "recoveryState": "needs_action", "errorCode": "rag_execution_interrupted", "runId": request.run_id, "paperId": "paper-1"}


def test_ask_gate_records_only_grounded_answer_and_corrupt_journal_requires_action_without_reexecution(tmp_path: Path) -> None:
    transcripts = PaperRagTranscriptFileStore(tmp_path / "rag")
    research = _JournalResearch(transcripts)
    service, _facade, store = _service(tmp_path, research)
    generation = service.generation()
    semantic = {
        "kind": "ask", "runId": "run-1", "paperId": "paper-1",
        "sourceRevision": REVISION, "sourceFingerprint": REVISION,
        "question": "What is verified?", "selection": {}, "locale": None,
    }
    research.response = {"answer": None, "abstained": True, "status": "abstained", "runId": "run-1", "paperId": "paper-1", "citations": []}
    rejected = service.submit_ask(
        OWNER, request_id="ask-gate-1", generation=generation,
        request_fingerprint=canonical_request_fingerprint(semantic), run_id="run-1", paper_id="paper-1",
        source_revision=REVISION, source_fingerprint=REVISION, question=semantic["question"],
    )
    assert rejected["status"] == "rejected"
    assert store.capability_verifications(runtime_identity=research.capability_runtime_identity()) == {}

    research.response = {
        "answer": "The method is grounded in the methods section.", "abstained": False, "status": "answered",
        "runId": "run-1", "paperId": "paper-1", "transcriptId": "rag-transcript://workflow-fixture/accepted",
        "decision": {"decision_type": "return_answer"},
        "citations": [{
            "id": "citation-1", "label": "Methods", "evidenceId": "evidence-methods-1",
            "paperId": "paper-1", "sourceRevision": REVISION, "sectionId": "methods", "valid": True,
        }],
        "evidenceRefs": ["evidence-methods-1"], "claims": [{"claimId": "claim-1", "citationIds": ["citation-1"]}],
        "metadata": {"generationMode": "gated_harness"},
    }
    accepted_semantic = {**semantic, "question": "Which method is verified?"}
    accepted = service.submit_ask(
        OWNER, request_id="ask-gate-2", generation=generation,
        request_fingerprint=canonical_request_fingerprint(accepted_semantic), run_id="run-1", paper_id="paper-1",
        source_revision=REVISION, source_fingerprint=REVISION, question=accepted_semantic["question"],
    )
    assert accepted["status"] == "passed"
    assert accepted["result"]["sourceRevision"] == REVISION
    assert accepted["result"]["citations"][0]["sectionId"] == "methods"
    assert accepted["result"]["citations"][0]["valid"] is True
    assert accepted["result"]["citations"][0]["evidenceId"] == "evidence-methods-1"
    assert accepted["result"]["evidenceRefs"] == ["evidence-methods-1"]
    assert accepted["result"]["transcriptId"] == "rag-transcript://workflow-fixture/accepted"
    assert accepted["result"]["answerStatus"] == "answered"
    assert not {"status", "abstained", "claims", "metadata"}.intersection(accepted["result"])

    interrupted_id = "ask-interrupted-1"
    store.admit(owner_id="alice", request_id=interrupted_id, generation=generation, kind="ask", fingerprint=canonical_request_fingerprint(semantic), semantic=semantic)
    store.bind_authority(owner_id="alice", request_id=interrupted_id, generation=generation, run_id="run-1")
    store.fail(owner_id="alice", request_id=interrupted_id, generation=generation, error={"code": "desktop_execution_interrupted"}, needs_review=True)
    journal = transcripts.journal_path_for(interrupted_id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('{"schema_version":"newsroom.paper_rag_transcript.v2","kind":"event_journal","session_id":"ask-interrupted-1","events":[]}', encoding="utf-8")
    before = research.ask_execution_count
    recovered = service.get_operation(OWNER, request_id=interrupted_id, generation=generation)
    assert recovered["status"] == "needs_action"
    assert recovered["automaticRetryAllowed"] is False
    assert research.ask_execution_count == before


class _ReportResearch(_ResearchFacade):
    def get_run_projection(self, run_id, *, actor):
        base = super().get_run_projection(run_id, actor=actor)
        source_ref = "source://paper-1/methods"
        return {
            **base,
            "disposition": "accepted",
            "analysis": {
                "paper_id": "paper-1",
                "summary": {
                    "problem": "Reliable paper analysis requires evidence.",
                    "core_idea": "Use deterministic gates around typed artifacts.",
                    "key_contributions": ["A bounded evidence workflow"],
                    "method_summary": "Retrieve evidence and verify it before publication.",
                    "experiment_summary": "The workflow fixture passes its deterministic gates.",
                    "limitations": ["Single paper fixture"],
                    "why_it_matters": "Accepted reports remain grounded in durable artifacts.",
                    "read_next": ["Inspect the methods evidence."],
                    "evidence_refs": [{"evidence_id": "evidence-1", "source_ref": source_ref, "section_id": "methods"}],
                    "confidence": 1.0,
                    "quality_flags": [],
                },
                "contributions": ["A bounded evidence workflow"],
                "methods": ["Bounded evidence retrieval"],
                "experiments": ["Deterministic contract fixture"],
                "limitations": ["Single paper fixture"],
                "reproducibility": ["Persist typed analysis and quality artifacts"],
                "related_work": [],
                "claims": [{
                    "claim_id": "claim-1",
                    "text": "The workflow uses bounded evidence retrieval.",
                    "claim_type": "method",
                    "section_id": "methods",
                    "evidence_ids": ["evidence-1"],
                    "confidence": 1.0,
                }],
                "evidence_pack_id": "evidence-pack-1",
                "quality": {"evidence_item_count": 1, "llm_candidate_warnings": []},
            },
            "quality": {
                "result_id": "quality-1", "target_id": "paper-1", "target_type": "summary",
                "passed": True, "score": 1.0, "gate_results": [], "quality_flags": [],
            },
            "reader": {
                "document": {"sections": [{"section_id": "methods", "source_ref": source_ref, "page_start": 1, "page_end": 1}]},
                "evidence": {"items": [{"evidence_id": "evidence-1", "source_ref": source_ref, "span_refs": ["section=methods&page=1"]}]},
            },
        }


def test_saved_report_reopens_and_exports_only_exact_accepted_run_revision_and_payload(tmp_path: Path) -> None:
    service, research, store = _service(tmp_path, _ReportResearch())
    saved = service.save_report(OWNER, report_id="report-1", run_id="run-1", source_revision=REVISION, title="Accepted report")
    assert saved["reference"]["runId"] == "run-1"

    rebuilt = ResearchDesktopApplicationService(
        research=research,
        requests=LocalJsonResearchDesktopRequestStore(store.path),
        imports=ResearchImportService(tmp_path / "imports"),
    )
    reopened = rebuilt.get_saved_report(OWNER, report_id="report-1")
    exported = rebuilt.export_report(OWNER, run_id="run-1", source_revision=REVISION, format="json")
    assert reopened["report"]["runId"] == "run-1"
    assert json.loads(exported["content"])["sourceRevision"] == REVISION

    variants = (
        {"artifactStatus": "corrupt", "eligibleForAcceptedExport": False},
        {"paperId": "other-paper"},
        {"sourceRevision": "sha256:" + "b" * 64},
        {"accepted": False, "eligibleForAcceptedExport": False},
    )
    original = research.get_run_projection
    for index, patch in enumerate(variants):
        research.get_run_projection = lambda run_id, *, actor, patch=patch: {**original(run_id, actor=actor), **patch}
        with pytest.raises(ResearchDesktopServiceError):
            rebuilt.save_report(OWNER, report_id=f"report-invalid-{index}", run_id="run-1", source_revision=REVISION, title="Invalid report")
    research.get_run_projection = original
    with pytest.raises(ResearchDesktopServiceError) as foreign:
        rebuilt.get_saved_report(OTHER_OWNER, report_id="report-1")
    assert foreign.value.code == "report_not_found"


def test_prepare_recovery_does_not_complete_from_unrelated_latest_document(tmp_path: Path) -> None:
    service, research, store = _service(tmp_path)
    generation = service.generation()
    semantic = _prepare_semantic()
    store.admit(
        owner_id="alice", request_id="prepare-live-1", generation=generation,
        kind="prepare", fingerprint=canonical_request_fingerprint(semantic), semantic=semantic,
    )
    store.bind_authority(
        owner_id="alice", request_id="prepare-live-1", generation=generation, run_id="parse-run-not-finalized",
    )
    research.identity = {"paperId": "paper-1", "sourceRevision": REVISION, "sourceFingerprint": REVISION}

    recovered = service.get_operation(OWNER, request_id="prepare-live-1", generation=generation)

    assert recovered["status"] in {"running", "needs_action"}
    assert recovered.get("result") is None
    assert research.parse_calls == []


def test_recovered_abstention_or_empty_answer_never_becomes_passed(tmp_path: Path) -> None:
    service, research, store = _service(tmp_path)
    generation = service.generation()
    semantic = {
        "kind": "ask", "runId": "run-1", "paperId": "paper-1",
        "sourceRevision": REVISION, "sourceFingerprint": REVISION,
        "question": "What is verified?", "selection": {}, "locale": None,
    }
    store.admit(
        owner_id="alice", request_id="ask-recovered-abstain", generation=generation,
        kind="ask", fingerprint=canonical_request_fingerprint(semantic), semantic=semantic,
    )
    store.bind_authority(
        owner_id="alice", request_id="ask-recovered-abstain", generation=generation, run_id="run-1",
    )
    research.recovery = {
        "answer": "", "abstained": True, "status": "abstained",
        "runId": "run-1", "paperId": "paper-1", "citations": [],
    }

    recovered = service.get_operation(OWNER, request_id="ask-recovered-abstain", generation=generation)

    assert recovered["status"] in {"rejected", "needs_action"}
    assert recovered["status"] != "passed"
    assert "paper-question-answering" not in store.capability_verifications(
        runtime_identity=research.capability_runtime_identity()
    )
