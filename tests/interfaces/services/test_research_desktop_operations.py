from __future__ import annotations

from pathlib import Path


from interfaces.services.research_desktop_control import (
    LocalJsonResearchDesktopRequestStore,
    canonical_request_fingerprint,
)
from interfaces.services.research_desktop_service import (
    ResearchDesktopApplicationService,
    ResearchDesktopOwner,
)
from interfaces.services.research_import_service import ResearchImportService
from interfaces.services.research_service import ResearchApplicationService


OWNER = ResearchDesktopOwner(user_id="alice")


class _ResearchFacade(ResearchApplicationService):
    def __init__(self) -> None:
        self.parse_calls = []
        self.completed_parse_runs = set()
        self.ask_calls = []
        self.analysis_calls = []
        self.recovery = None
        self.identity = {
            "paperId": "paper-1",
            "sourceRevision": "sha256:" + "a" * 64,
            "sourceFingerprint": "sha256:" + "a" * 64,
        }

    def capability_runtime_identity(self):
        return "sha256:" + "f" * 64

    def capability_projection(self):
        return {"catalog": True, "parse": True, "analyze": True, "ask": True, "ragAsk": True, "artifactRead": True}

    def parse_paper(self, command):
        if command.run_id not in self.completed_parse_runs:
            self.parse_calls.append(command)
            self.completed_parse_runs.add(command.run_id)
        return {"paperId": command.metadata["expected_paper_id"], "status": "ready"}

    def get_document_identity(self, paper_id, *, actor):
        assert actor.user_id == "alice"
        return {**self.identity, "paperId": paper_id}

    def recover_prepared_source(self, command):
        if command.run_id not in self.completed_parse_runs:
            return None
        return {"status": "ready", **self.identity, "paperId": command.metadata["expected_paper_id"]}

    def resolve_analyze_source(self, paper_id, *, source_revision, source_fingerprint, actor):
        assert actor.user_id == "alice"
        assert source_revision == source_fingerprint
        return "file:///immutable/catalog/source.pdf"

    def analyze_paper(self, command):
        self.analysis_calls.append(command)
        return {"runId": command.run_id, "paperId": command.paper_id, "status": "succeeded"}

    def get_run_projection(self, run_id, *, actor):
        assert actor.user_id == "alice"
        return {
            "runId": run_id, "paperId": "paper-1", "status": "succeeded",
            "accepted": True, "qualityPassed": True,
            "eligibleForAcceptedExport": True, "artifactStatus": "valid",
            "sourceRevision": "sha256:" + "a" * 64,
            "sourceFingerprint": "sha256:" + "a" * 64,
        }

    def ask_paper(self, paper_id, request):
        self.ask_calls.append((paper_id, request))
        return {
            "answer": "Grounded answer", "abstained": False,
            "citations": [{
                "id": "citation:1", "label": "Methods", "paperId": paper_id,
                "sourceRevision": "sha256:" + "a" * 64,
                "valid": True, "sectionId": "methods",
            }],
            "evidenceRefs": ["citation:1"], "claims": [], "confidence": 1.0,
            "transcriptId": "transcript-1", "status": "answered",
            "runId": request.run_id, "paperId": paper_id,
            "metadata": {"retrievalGoalId": "goal-1", "generationMode": "gated_harness", "recovered": False},
        }

    def recover_paper_answer(self, paper_id, request, *, include_interrupted=False):
        del paper_id, request, include_interrupted
        return self.recovery


def _service(tmp_path: Path, research: _ResearchFacade | None = None):
    facade = research or _ResearchFacade()
    request_store = LocalJsonResearchDesktopRequestStore(tmp_path / "control.json")
    imports = ResearchImportService(tmp_path / "imports")
    service = ResearchDesktopApplicationService(
        research=facade,
        requests=request_store,
        imports=imports,
    )
    return service, facade, request_store


def test_prepare_is_durable_idempotent_and_rebuild_uses_catalog_authority(tmp_path: Path) -> None:
    service, research, store = _service(tmp_path)
    generation = service.generation()
    semantic = {
        "kind": "prepare", "paperId": "paper-1",
        "sourceUrl": "https://example.test/论文.pdf", "sourceType": None,
    }
    fingerprint = canonical_request_fingerprint(semantic)

    first = service.submit_prepare(
        OWNER, request_id="prepare-0001", generation=generation,
        request_fingerprint=fingerprint, paper_id="paper-1",
        source_url=semantic["sourceUrl"], source_type=None,
    )
    duplicate = service.submit_prepare(
        OWNER, request_id="prepare-0001", generation=generation,
        request_fingerprint=fingerprint, paper_id="paper-1",
        source_url=semantic["sourceUrl"], source_type=None,
    )

    assert first["status"] == duplicate["status"] == "passed"
    assert first["result"]["sourceFingerprint"] == "sha256:" + "a" * 64
    assert len(research.parse_calls) == 1

    rebuilt = ResearchDesktopApplicationService(
        research=research,
        requests=LocalJsonResearchDesktopRequestStore(store.path),
        imports=ResearchImportService(tmp_path / "imports"),
    )
    queried = rebuilt.get_operation(
        OWNER, request_id="prepare-0001", generation=generation
    )
    assert queried["status"] == "passed"
    assert len(research.parse_calls) == 1

    lost_request = "prepare-0002"
    lost_run = "research-desktop-authority-run"
    store.admit(
        owner_id="alice", request_id=lost_request, generation=generation,
        kind="prepare", fingerprint=fingerprint, semantic=semantic,
    )
    store.bind_authority(
        owner_id="alice", request_id=lost_request, generation=generation, run_id=lost_run,
    )
    research.completed_parse_runs.add(lost_run)
    recovered = rebuilt.get_operation(
        OWNER, request_id=lost_request, generation=generation
    )
    assert recovered["status"] == "passed"
    assert recovered["runId"] == lost_run
    assert recovered["result"]["sourceRevision"] == "sha256:" + "a" * 64
    assert len(research.parse_calls) == 1


def test_private_analysis_uses_verified_catalog_locator_and_fixture_null_semantic(tmp_path: Path) -> None:
    service, research, _store = _service(tmp_path)
    content = b"%PDF-1.7\nprivate"
    digest = "sha256:" + __import__("hashlib").sha256(content).hexdigest()
    received = service._imports.create_received(
        user_id="alice", content=content, filename="论文.pdf",
        source_revision=digest, source_fingerprint=digest,
    )
    service._imports.mark_completed(
        user_id="alice", import_id=received["importId"], result={"paperId": "paper-1"}
    )
    research.identity = {"paperId": "paper-1", "sourceRevision": digest, "sourceFingerprint": digest}
    generation = service.generation()
    semantic = {
        "kind": "analyze", "paperId": "paper-1", "sourceRevision": digest,
        "sourceFingerprint": digest, "sourceUrl": None, "pdfUrl": None,
        "objective": None, "constraints": ["保留原质量门与真实证据范围"],
    }

    result = service.submit_analysis(
        OWNER, request_id="analyze-0001", generation=generation,
        request_fingerprint=canonical_request_fingerprint(semantic),
        paper_id="paper-1", source_revision=digest, source_fingerprint=digest,
        objective=None, constraints=semantic["constraints"],
    )

    assert result["status"] == "passed"
    assert research.analysis_calls[0].pdf_url == "file:///immutable/catalog/source.pdf"
    assert (
        research.analysis_calls[0].options["research_input_objective"]
        == "Analyze paper paper-1"
    )


def test_ask_abstention_is_rejected_and_interrupted_recovery_never_reexecutes(tmp_path: Path) -> None:
    service, research, store = _service(tmp_path)
    generation = service.generation()
    semantic = {
        "kind": "ask", "runId": "run-1", "paperId": "paper-1",
        "sourceRevision": "sha256:" + "a" * 64,
        "sourceFingerprint": "sha256:" + "a" * 64,
        "question": "Why?", "selection": {}, "locale": None,
    }
    fingerprint = canonical_request_fingerprint(semantic)
    research.ask_paper = lambda paper_id, request: {
        "answer": None, "abstained": True, "status": "abstained",
        "runId": request.run_id, "paperId": paper_id,
    }
    rejected = service.submit_ask(
        OWNER, request_id="ask-0001", generation=generation,
        request_fingerprint=fingerprint, run_id="run-1", paper_id="paper-1",
        source_revision=semantic["sourceRevision"],
        source_fingerprint=semantic["sourceFingerprint"], question="Why?",
    )
    assert rejected["status"] == "rejected"
    assert "paper-question-answering" not in store.capability_verifications(
        runtime_identity=research.capability_runtime_identity()
    )

    store.admit(
        owner_id="alice", request_id="ask-0002", generation=generation,
        kind="ask", fingerprint=fingerprint, semantic=semantic,
    )
    store.bind_authority(
        owner_id="alice", request_id="ask-0002", generation=generation, run_id="run-1"
    )
    store.fail(
        owner_id="alice", request_id="ask-0002", generation=generation,
        error={"code": "desktop_execution_interrupted"}, needs_review=True,
    )
    research.recovery = {
        "status": "interrupted", "recoveryState": "needs_action",
        "errorCode": "rag_execution_interrupted", "runId": "run-1", "paperId": "paper-1",
    }
    recovered = service.get_operation(
        OWNER, request_id="ask-0002", generation=generation
    )
    assert recovered["status"] == "needs_action"
    assert recovered["automaticRetryAllowed"] is False
