from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace
from typing import Any

import pytest

import infrastructure.research.diagnostics as diagnostics_module
import infrastructure.research.filesystem_run_store as run_store_module
from backend.research.application import AnalyzePaperRequest, AnalyzePaperUseCase
from backend.research.application.run_disposition import research_identity_scope_ref
from backend.research.application.single_paper_runtime import (
    ResearchAnalysisResult,
    ResearchSinglePaperRuntime,
)
from backend.research.ports.run_store import (
    ResearchRunDisposition,
    ResearchRunDispositionReason,
    ResearchRunRecord,
    ResearchRunStore,
    ResearchRunStoreConflictError,
    ResearchRunStoreCorruptionError,
    ResearchRunStoreError,
    ResearchRunStoreReason,
    ResearchRunStoreUnavailableError,
    ResearchRunStoreValidationError,
)
from infrastructure.research.filesystem_run_store import (
    RESEARCH_RUN_LATEST_INDEX_SCHEMA_VERSION,
    RESEARCH_RUN_LATEST_INDEX_SCHEMA_VERSION_V2,
    RESEARCH_RUN_RECORD_SCHEMA_VERSION,
    RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    FilesystemResearchRunStore,
)
from infrastructure.research.diagnostics import (
    MISSING_IDENTITY_REF,
    RESEARCH_PERSISTENCE_LOGGER,
    RESEARCH_PERSISTENCE_OPERATION_EVENT,
    SAFE_DIAGNOSTIC_LABEL,
    emit_research_persistence_diagnostic,
)
from framework.harness import FakeArtifactPort, InMemoryHarnessEventPort
from tests.backend.research.fakes import (
    FakeGithubRepositoryPort,
    FakeResearchDocumentCompiler,
    FakeResearchLLMWorker,
    FakeResearchRAGRuntime,
    FakeResearchSourceProvider,
    in_memory_node_output_resource_factory,
)


@dataclass(frozen=True)
class _StoredResult:
    run_id: str
    paper_id: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": "succeeded",
            "analysis": {
                "paper_id": self.paper_id,
                "summary": self.value,
            },
            "quality": {
                "target_id": self.paper_id,
                "passed": True,
            },
            "artifact_refs": {
                "research-analysis": f"artifact://{self.run_id}/analysis",
                "research-quality-result": f"artifact://{self.run_id}/quality",
                "harness-trace": f"artifact://{self.run_id}/trace",
                "harness-transcript": f"artifact://{self.run_id}/transcript",
            },
            "trace": {
                "run_id": self.run_id,
                "events": [{"event_type": "completed", "value": self.value}],
            },
            "transcript": {
                "run_id": self.run_id,
                "entries": [{"phase": "VERIFY"}],
            },
            "actor_scope": {"memory_namespace": "research.public"},
            "trace_ref": f"harness-trace://{self.run_id}",
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> _StoredResult:
        return cls(
            run_id=value["run_id"],
            paper_id=value["analysis"]["paper_id"],
            value=value["analysis"]["summary"],
        )


def _record(
    run_id: str, paper_id: str = "paper-1", value: str | None = None
) -> ResearchRunRecord:
    return ResearchRunRecord(
        run_id=run_id,
        paper_id=paper_id,
        result=_StoredResult(
            run_id=run_id,
            paper_id=paper_id,
            value=value or run_id,
        ),
    )


def _record_path(store: FilesystemResearchRunStore, run_id: str) -> Path:
    filename = f"{sha256(run_id.encode('utf-8')).hexdigest()}.json"
    return store.records_root / filename


def _index_path(store: FilesystemResearchRunStore, paper_id: str) -> Path:
    filename = f"{sha256(paper_id.encode('utf-8')).hexdigest()}.json"
    return store.latest_root / filename


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _save_in_process(root: str, run_id: str, barrier: Any) -> None:
    store = FilesystemResearchRunStore(
        root,
        result_decoder=_StoredResult.from_dict,
    )
    barrier.wait(timeout=20)
    store.save(_record(run_id))


def _identity_decoder(value: dict[str, Any]) -> dict[str, Any]:
    return dict(value)


def _disposition_result(
    run_id: str,
    *,
    paper_id: str = "paper-disposition",
    status: str = "succeeded",
    quality_passed: bool = True,
    authority_ref: str | None = None,
    scope: dict[str, str] | None = None,
) -> dict[str, Any]:
    actor_scope = scope or {"memory_namespace": "research.public"}
    diagnostics: dict[str, Any] = {}
    if authority_ref is not None:
        diagnostics["publication_authority_ref"] = authority_ref
    return {
        "run_id": run_id,
        "status": status,
        "analysis": {"paper_id": paper_id, "summary": run_id},
        "quality": {"target_id": paper_id, "passed": quality_passed},
        "artifact_refs": {
            "research-analysis": f"artifact://{run_id}/research-analysis",
            "research-quality-result": f"artifact://{run_id}/research-quality-result",
            "harness-trace": f"artifact://{run_id}/harness-trace",
            "harness-transcript": f"artifact://{run_id}/harness-transcript",
        },
        "actor_scope": dict(actor_scope),
        "trace": {"run_id": run_id, "metadata": dict(actor_scope)},
        "transcript": {"run_id": run_id, "entries": []},
        "diagnostics": diagnostics,
        "trace_ref": f"harness-trace://{run_id}",
    }


def _disposition_record(
    run_id: str,
    *,
    paper_id: str = "paper-disposition",
    status: str = "succeeded",
    quality_passed: bool = True,
    authority_ref: str | None = None,
    scope: dict[str, str] | None = None,
) -> ResearchRunRecord:
    return ResearchRunRecord(
        run_id=run_id,
        paper_id=paper_id,
        result=_disposition_result(
            run_id,
            paper_id=paper_id,
            status=status,
            quality_passed=quality_passed,
            authority_ref=authority_ref,
            scope=scope,
        ),
        publication_authority_ref=authority_ref,
    )


def test_run_store_port_and_errors_have_stable_sanitized_contract(
    tmp_path: Path,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )

    assert isinstance(store, ResearchRunStore)
    assert issubclass(ResearchRunStoreValidationError, ValueError)
    assert issubclass(ResearchRunStoreCorruptionError, ResearchRunStoreError)
    assert issubclass(ResearchRunStoreConflictError, ResearchRunStoreError)
    assert issubclass(ResearchRunStoreUnavailableError, ResearchRunStoreError)

    error = ResearchRunStoreCorruptionError(ResearchRunStoreReason.CHECKSUM_INVALID)
    assert error.to_public_dict() == {
        "code": "research_run_store_corrupt",
        "message": "Research run storage data failed integrity validation.",
        "reason": "checksum_invalid",
        "retryable": False,
    }
    assert "checksum" not in str(error).casefold()

    record = _record("run-frozen")
    with pytest.raises(FrozenInstanceError):
        record.run_id = "mutated"  # type: ignore[misc]


def test_round_trip_restart_and_hashed_latest_index(tmp_path: Path) -> None:
    paper_id = "hep-th/9901001"
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    first = _record("run-1", paper_id, "first")
    second = _record("run-2", paper_id, "second")

    store.save(first)
    store.save(second)

    record_state = _read_json(_record_path(store, "run-2"))
    assert set(record_state) == run_store_module._RECORD_FIELDS
    assert record_state["schema_version"] == RESEARCH_RUN_RECORD_SCHEMA_VERSION
    assert record_state["result_checksum"].startswith("sha256:")
    assert record_state["checksum"].startswith("sha256:")
    index_path = _index_path(store, paper_id)
    index_state = _read_json(index_path)
    assert set(index_state) == run_store_module._INDEX_FIELDS
    assert index_state["schema_version"] == RESEARCH_RUN_LATEST_INDEX_SCHEMA_VERSION
    assert index_state["run_id"] == "run-2"
    assert index_state["record_checksum"] == record_state["checksum"]
    assert paper_id not in index_path.name
    assert "/" not in index_path.name

    reopened = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    assert reopened.get_by_run_id("run-1") == first
    assert reopened.get_latest_by_paper_id(paper_id) == second
    assert reopened.get_by_run_id("missing") is None
    assert reopened.get_latest_by_paper_id("missing-paper") is None


def test_real_research_result_survives_restart_without_losing_replay_history(
    tmp_path: Path,
) -> None:
    runtime = ResearchSinglePaperRuntime(
        source_provider=FakeResearchSourceProvider(),
        document_compiler=FakeResearchDocumentCompiler(),
        llm_worker=FakeResearchLLMWorker(),
        github_repository=FakeGithubRepositoryPort(),
        rag_runtime=FakeResearchRAGRuntime(),
        artifact_port=FakeArtifactPort(),
        event_port_factory=lambda _run_id: InMemoryHarnessEventPort(),
        node_output_resource_factory=in_memory_node_output_resource_factory,
    )
    result = AnalyzePaperUseCase(runtime).analyze(
        AnalyzePaperRequest(
            run_id="research-run-persistence-store",
            paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123",
            user_id="user-1",
        )
    )
    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=ResearchAnalysisResult.from_dict,
    )
    writer.save(
        ResearchRunRecord(
            run_id=result.run_id,
            paper_id="paper-harness-001",
            result=result,
        )
    )

    reopened = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=ResearchAnalysisResult.from_dict,
    )
    restored = reopened.get_by_run_id(result.run_id)

    assert restored is not None
    assert isinstance(restored.result, ResearchAnalysisResult)
    assert restored.result.to_persistence_dict() == result.to_persistence_dict()
    assert sum(
        event.deterministic_history is not None
        for event in restored.result.trace.events
    ) == sum(event.deterministic_history is not None for event in result.trace.events)

    path = _record_path(reopened, result.run_id)
    state = _read_json(path)
    state["result"]["trace"]["metadata"]["paper_id"] = "foreign-paper"
    state["result_checksum"] = run_store_module._state_checksum(state["result"])
    unsigned = {
        key: state[key]
        for key in run_store_module._RECORD_FIELDS - {"checksum"}
    }
    state["checksum"] = run_store_module._state_checksum(unsigned)
    _write_json(path, state)

    with pytest.raises(ResearchRunStoreCorruptionError):
        reopened.get_by_run_id(result.run_id)


def test_missing_actor_scope_is_corruption_even_when_checksums_are_recomputed(
    tmp_path: Path,
) -> None:
    runtime = ResearchSinglePaperRuntime(
        source_provider=FakeResearchSourceProvider(),
        document_compiler=FakeResearchDocumentCompiler(),
        llm_worker=FakeResearchLLMWorker(),
        github_repository=FakeGithubRepositoryPort(),
        rag_runtime=FakeResearchRAGRuntime(),
        artifact_port=FakeArtifactPort(),
        event_port_factory=lambda _run_id: InMemoryHarnessEventPort(),
        node_output_resource_factory=in_memory_node_output_resource_factory,
    )
    result = AnalyzePaperUseCase(runtime).analyze(
        AnalyzePaperRequest(
            run_id="research-run-missing-actor-scope",
            paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    )
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=ResearchAnalysisResult.from_dict,
    )
    store.save(
        ResearchRunRecord(
            run_id=result.run_id,
            paper_id="paper-harness-001",
            result=result,
        )
    )

    path = _record_path(store, result.run_id)
    state = _read_json(path)
    state["result"].pop("actor_scope")
    state["result_checksum"] = run_store_module._state_checksum(state["result"])
    unsigned = {
        key: state[key]
        for key in run_store_module._RECORD_FIELDS - {"checksum"}
    }
    state["checksum"] = run_store_module._state_checksum(unsigned)
    _write_json(path, state)

    with pytest.raises(ResearchRunStoreCorruptionError) as failed:
        store.get_by_run_id(result.run_id)

    assert failed.value.reason is ResearchRunStoreReason.CONTENT_INVALID


def test_halted_research_result_survives_durable_round_trip(tmp_path: Path) -> None:
    runtime = ResearchSinglePaperRuntime(
        source_provider=FakeResearchSourceProvider(),
        document_compiler=FakeResearchDocumentCompiler(),
        llm_worker=FakeResearchLLMWorker(),
        github_repository=FakeGithubRepositoryPort(),
        rag_runtime=FakeResearchRAGRuntime(missing_required_evidence=True),
        artifact_port=FakeArtifactPort(),
        event_port_factory=lambda _run_id: InMemoryHarnessEventPort(),
        node_output_resource_factory=in_memory_node_output_resource_factory,
    )
    result = AnalyzePaperUseCase(runtime).analyze(
        AnalyzePaperRequest(
            run_id="research-run-halted-persistence",
            paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123",
            options={"max_replans": 0, "rag_max_replans": 0},
        )
    )
    assert result.status == "halted"
    assert result.quality.quality_flags

    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=ResearchAnalysisResult.from_dict,
    )
    writer.save(
        ResearchRunRecord(
            run_id=result.run_id,
            paper_id="paper-harness-001",
            result=result,
        )
    )

    reopened = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=ResearchAnalysisResult.from_dict,
    )
    restored = reopened.get_by_run_id(result.run_id)

    assert restored is not None
    assert restored.result.status == "halted"
    assert restored.result.to_persistence_dict() == result.to_persistence_dict()


def test_save_is_idempotent_but_rejects_run_identity_conflicts(tmp_path: Path) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    original = _record("run-1", "paper-1", "original")
    store.save(original)
    committed = _record_path(store, "run-1").read_bytes()

    store.save(original)
    assert _record_path(store, "run-1").read_bytes() == committed

    with pytest.raises(ResearchRunStoreConflictError) as changed_result:
        store.save(_record("run-1", "paper-1", "changed"))
    assert changed_result.value.reason is ResearchRunStoreReason.IDENTITY_CONFLICT

    with pytest.raises(ResearchRunStoreConflictError):
        store.save(_record("run-1", "paper-2", "original"))
    assert store.get_latest_by_paper_id("paper-1") == original
    assert store.get_latest_by_paper_id("paper-2") is None


@pytest.mark.parametrize("identity", ["", " run-1", "run-1 ", "run\n1"])
def test_invalid_identities_are_rejected_without_creating_records(
    tmp_path: Path,
    identity: str,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )

    with pytest.raises(ResearchRunStoreValidationError):
        store.save(_record(identity))

    assert (
        list(store.records_root.glob("*.json")) == []
        if store.records_root.exists()
        else True
    )


def test_non_finite_and_oversized_results_are_rejected(tmp_path: Path) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=dict,
        max_record_bytes=1_024,
    )
    non_finite = ResearchRunRecord(
        run_id="run-nan",
        paper_id="paper-1",
        result={
            "run_id": "run-nan",
            "analysis": {"paper_id": "paper-1", "score": float("nan")},
        },
    )
    with pytest.raises(ResearchRunStoreValidationError) as invalid:
        store.save(non_finite)
    assert invalid.value.reason is ResearchRunStoreReason.SERIALIZATION_FAILED

    with pytest.raises(ResearchRunStoreValidationError) as oversized:
        store.save(_record("run-large", value="x" * 2_000))
    assert oversized.value.reason is ResearchRunStoreReason.RECORD_TOO_LARGE
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize("max_record_bytes", [0, 1_023, 536_870_913, 2**80])
def test_record_size_configuration_is_bounded(
    tmp_path: Path,
    max_record_bytes: int,
) -> None:
    with pytest.raises(ResearchRunStoreValidationError) as exc_info:
        FilesystemResearchRunStore(
            tmp_path / "not-created",
            result_decoder=_StoredResult.from_dict,
            max_record_bytes=max_record_bytes,
        )

    assert exc_info.value.reason is ResearchRunStoreReason.INVALID_CONFIGURATION
    assert (tmp_path / "not-created").exists() is False


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda state: state["result"]["analysis"].update(summary="tampered"),
            ResearchRunStoreReason.CHECKSUM_INVALID,
        ),
        (
            lambda state: state.update(schema_version="unknown.v99"),
            ResearchRunStoreReason.SCHEMA_UNSUPPORTED,
        ),
        (
            lambda state: state.update(paper_id="foreign-paper"),
            ResearchRunStoreReason.IDENTITY_MISMATCH,
        ),
        (
            lambda state: state.update(extra="field"),
            ResearchRunStoreReason.SCHEMA_INVALID,
        ),
    ],
)
def test_tampered_record_fails_closed(
    tmp_path: Path,
    mutate: Any,
    reason: ResearchRunStoreReason,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-1"))
    path = _record_path(store, "run-1")
    state = _read_json(path)
    mutate(state)
    _write_json(path, state)

    with pytest.raises(ResearchRunStoreCorruptionError) as exc_info:
        store.get_by_run_id("run-1")
    assert exc_info.value.reason is reason


def test_malformed_json_and_non_regular_record_fail_closed(tmp_path: Path) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-malformed"))
    _record_path(store, "run-malformed").write_bytes(b"{not-json")

    with pytest.raises(ResearchRunStoreCorruptionError) as malformed:
        store.get_by_run_id("run-malformed")
    assert malformed.value.reason is ResearchRunStoreReason.CONTENT_INVALID

    other = FilesystemResearchRunStore(
        tmp_path / "other",
        result_decoder=_StoredResult.from_dict,
    )
    other.records_root.mkdir(parents=True)
    _record_path(other, "run-directory").mkdir()
    with pytest.raises(ResearchRunStoreCorruptionError):
        other.get_by_run_id("run-directory")


def test_duplicate_json_object_key_is_rejected(tmp_path: Path) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-duplicate-key"))
    path = _record_path(store, "run-duplicate-key")
    committed = path.read_text(encoding="utf-8").lstrip()
    path.write_text(
        '{"run_id":"shadow-run",' + committed[1:],
        encoding="utf-8",
    )

    with pytest.raises(ResearchRunStoreCorruptionError) as exc_info:
        store.get_by_run_id("run-duplicate-key")
    assert exc_info.value.reason is ResearchRunStoreReason.CONTENT_INVALID


def test_record_file_identity_swap_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-1"))
    path = _record_path(store, "run-1")
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(path.read_bytes())
    real_open = Path.open

    def open_replacement(selected: Path, *args: Any, **kwargs: Any) -> Any:
        target = replacement if selected == path else selected
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_replacement)
    with pytest.raises(ResearchRunStoreCorruptionError) as exc_info:
        store.get_by_run_id("run-1")
    assert exc_info.value.reason is ResearchRunStoreReason.IDENTITY_MISMATCH


def test_record_replace_failure_preserves_prior_latest_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-1"))
    prior_index = _index_path(store, "paper-1").read_bytes()

    def fail_replace(_source: Any, _destination: Any) -> None:
        raise OSError("private filesystem failure")

    monkeypatch.setattr(run_store_module.os, "replace", fail_replace)
    with pytest.raises(ResearchRunStoreUnavailableError) as exc_info:
        store.save(_record("run-2"))

    assert "private" not in str(exc_info.value)
    assert _index_path(store, "paper-1").read_bytes() == prior_index
    assert store.get_by_run_id("run-2") is None
    assert store.get_latest_by_paper_id("paper-1") == _record("run-1")
    assert list(tmp_path.rglob("*.tmp")) == []


def test_index_replace_failure_keeps_committed_record_and_retry_repairs_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-1"))
    prior_index = _index_path(store, "paper-1").read_bytes()
    real_replace = run_store_module.os.replace

    def fail_index_replace(source: Any, destination: Any) -> None:
        if Path(destination).parent == store.latest_root:
            raise OSError("private index failure")
        real_replace(source, destination)

    monkeypatch.setattr(run_store_module.os, "replace", fail_index_replace)
    with pytest.raises(ResearchRunStoreUnavailableError):
        store.save(_record("run-2"))

    assert _index_path(store, "paper-1").read_bytes() == prior_index
    assert store.get_by_run_id("run-2") == _record("run-2")
    with pytest.raises(ResearchRunStoreUnavailableError):
        store.get_latest_by_paper_id("paper-1")

    monkeypatch.setattr(run_store_module.os, "replace", real_replace)
    reopened = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    reopened.save(_record("run-2"))
    assert reopened.get_latest_by_paper_id("paper-1") == _record("run-2")
    assert _read_json(_index_path(reopened, "paper-1"))["run_id"] == "run-2"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_valid_but_stale_index_is_recovered_from_committed_records(
    tmp_path: Path,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-1"))
    store.save(_record("run-2"))
    path = _index_path(store, "paper-1")
    state = _read_json(path)
    state["run_id"] = "missing-run"
    state["record_checksum"] = f"sha256:{'0' * 64}"
    unsigned = {
        key: state[key] for key in run_store_module._INDEX_FIELDS - {"checksum"}
    }
    state["checksum"] = run_store_module._state_checksum(unsigned)
    _write_json(path, state)

    assert store.get_latest_by_paper_id("paper-1") == _record("run-2")
    assert _read_json(path)["run_id"] == "run-2"


def test_invalid_index_checksum_fails_closed_instead_of_silent_repair(
    tmp_path: Path,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    store.save(_record("run-1"))
    path = _index_path(store, "paper-1")
    state = _read_json(path)
    state["run_id"] = "tampered"
    _write_json(path, state)

    with pytest.raises(ResearchRunStoreCorruptionError) as exc_info:
        store.get_latest_by_paper_id("paper-1")
    assert exc_info.value.reason is ResearchRunStoreReason.CHECKSUM_INVALID
    assert _read_json(path)["run_id"] == "tampered"


def test_decoder_failure_is_sanitized_and_lossy_decoder_fails_closed(
    tmp_path: Path,
) -> None:
    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    writer.save(_record("run-1"))

    def fail_decoder(_value: dict[str, Any]) -> Any:
        raise RuntimeError("secret=C:/private/run-store")

    broken = FilesystemResearchRunStore(tmp_path, result_decoder=fail_decoder)
    with pytest.raises(ResearchRunStoreCorruptionError) as failed:
        broken.get_by_run_id("run-1")
    assert "secret" not in str(failed.value)
    assert failed.value.__cause__ is None

    def lossy_decoder(value: dict[str, Any]) -> _StoredResult:
        restored = _StoredResult.from_dict(value)
        return _StoredResult(restored.run_id, restored.paper_id, "changed")

    lossy = FilesystemResearchRunStore(tmp_path, result_decoder=lossy_decoder)
    with pytest.raises(ResearchRunStoreCorruptionError):
        lossy.get_by_run_id("run-1")


@pytest.mark.parametrize("mode", ["raises", "lossy", "foreign_identity"])
def test_invalid_decoder_rejects_save_before_filesystem_side_effects(
    tmp_path: Path,
    mode: str,
) -> None:
    def invalid_decoder(value: dict[str, Any]) -> _StoredResult:
        if mode == "raises":
            raise RuntimeError("secret=C:/private/run-store")
        restored = _StoredResult.from_dict(value)
        if mode == "foreign_identity":
            return _StoredResult("foreign-run", restored.paper_id, restored.value)
        return _StoredResult(restored.run_id, restored.paper_id, "changed")

    root = tmp_path / "not-created"
    store = FilesystemResearchRunStore(root, result_decoder=invalid_decoder)

    with pytest.raises(ResearchRunStoreValidationError) as exc_info:
        store.save(_record("run-invalid-decoder"))

    assert "secret" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert root.exists() is False


def test_reentrant_decoder_does_not_deadlock_on_store_read(tmp_path: Path) -> None:
    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    writer.save(_record("run-1"))
    holder: dict[str, FilesystemResearchRunStore] = {}

    def reentrant_decoder(value: dict[str, Any]) -> _StoredResult:
        assert holder["store"].get_by_run_id("missing") is None
        return _StoredResult.from_dict(value)

    reader = FilesystemResearchRunStore(tmp_path, result_decoder=reentrant_decoder)
    holder["store"] = reader
    completed: list[ResearchRunRecord | None] = []
    thread = Thread(target=lambda: completed.append(reader.get_by_run_id("run-1")))
    thread.start()
    thread.join(timeout=5)

    assert thread.is_alive() is False
    assert completed == [_record("run-1")]


def test_slow_decoder_does_not_hold_store_lock_or_block_writer(tmp_path: Path) -> None:
    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    writer.save(_record("run-1"))
    decoder_started = Event()
    release_decoder = Event()

    def slow_decoder(value: dict[str, Any]) -> _StoredResult:
        decoder_started.set()
        if not release_decoder.wait(timeout=10):
            raise TimeoutError("test decoder was not released")
        return _StoredResult.from_dict(value)

    reader = FilesystemResearchRunStore(tmp_path, result_decoder=slow_decoder)
    read_results: list[ResearchRunRecord | None] = []
    read_thread = Thread(
        target=lambda: read_results.append(reader.get_by_run_id("run-1"))
    )
    read_thread.start()
    assert decoder_started.wait(timeout=5)

    write_thread = Thread(target=lambda: writer.save(_record("run-2")))
    write_thread.start()
    write_thread.join(timeout=5)

    try:
        assert write_thread.is_alive() is False
        assert writer.get_by_run_id("run-2") == _record("run-2")
    finally:
        release_decoder.set()
        read_thread.join(timeout=5)
    assert read_thread.is_alive() is False
    assert read_results == [_record("run-1")]


def test_concurrent_instances_commit_isolated_records_and_one_valid_latest(
    tmp_path: Path,
) -> None:
    stores = [
        FilesystemResearchRunStore(
            tmp_path,
            result_decoder=_StoredResult.from_dict,
        )
        for _ in range(2)
    ]
    run_ids = [f"thread-{index:02d}" for index in range(16)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(stores[index % 2].save, _record(run_id))
            for index, run_id in enumerate(run_ids)
        ]
        for future in futures:
            future.result(timeout=20)

    for run_id in run_ids:
        assert stores[0].get_by_run_id(run_id) == _record(run_id)
    latest = stores[1].get_latest_by_paper_id("paper-1")
    assert latest is not None
    assert latest.run_id in run_ids
    index = _read_json(_index_path(stores[0], "paper-1"))
    committed = _read_json(_record_path(stores[0], index["run_id"]))
    assert index["record_checksum"] == committed["checksum"]
    assert index["commit_generation"] == len(run_ids)


def test_concurrent_processes_serialize_latest_index_updates(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    process_count = 4
    barrier = context.Barrier(process_count)
    run_ids = [f"process-{index}" for index in range(process_count)]
    processes = [
        context.Process(
            target=_save_in_process,
            args=(str(tmp_path), run_id, barrier),
        )
        for run_id in run_ids
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    reopened = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    for run_id in run_ids:
        assert reopened.get_by_run_id(run_id) == _record(run_id)
    latest = reopened.get_latest_by_paper_id("paper-1")
    assert latest is not None
    assert latest.run_id in run_ids
    assert (
        _read_json(_index_path(reopened, "paper-1"))["commit_generation"]
        == process_count
    )


def test_windows_file_lock_handles_competitor_after_empty_file_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force a competing owner between the empty-file stat and acquisition."""

    lock_path = tmp_path / "windows-empty-lock"
    lock_path.touch()
    original_identity = lock_path.stat()
    empty_observed = Event()
    competitor_entered = Event()
    release_competitor = Event()
    competitor_finished = Event()
    contended = Event()
    state_guard = Lock()
    owner: int | None = None
    acquisition_order: list[str] = []
    unlocks: list[int] = []
    writes: list[int] = []
    errors: list[Exception] = []

    class FakeMsvcrt:
        LK_NBLCK = 2
        LK_UNLCK = 0

        @staticmethod
        def locking(descriptor: int, mode: int, size: int) -> None:
            nonlocal owner
            assert size == 1
            assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
            with state_guard:
                if mode == FakeMsvcrt.LK_UNLCK:
                    assert owner == descriptor
                    owner = None
                    unlocks.append(descriptor)
                    return
                assert mode == FakeMsvcrt.LK_NBLCK
                if owner is not None:
                    contended.set()
                    raise PermissionError("competing process owns byte zero")
                owner = descriptor

    def observed_fstat(descriptor: int) -> os.stat_result:
        opened = os.fstat(descriptor)
        if current_thread() is primary:
            assert opened.st_size == 0
            empty_observed.set()
            assert competitor_entered.wait(timeout=5)
        return opened

    def checked_write(descriptor: int, data: bytes) -> int:
        with state_guard:
            writes.append(descriptor)
            if owner is not None and owner != descriptor:
                raise PermissionError("initialization write crosses a locked byte")
        return os.write(descriptor, data)

    def contention_poll(seconds: float) -> None:
        assert 0 < seconds <= run_store_module._WINDOWS_LOCK_POLL_SECONDS
        assert contended.is_set()
        release_competitor.set()
        assert competitor_finished.wait(timeout=5)

    def enter_primary() -> None:
        try:
            with run_store_module._exclusive_file_lock(lock_path):
                acquisition_order.append("primary")
        except Exception as exc:
            errors.append(exc)

    def enter_competitor() -> None:
        try:
            assert empty_observed.wait(timeout=5)
            with run_store_module._exclusive_file_lock(lock_path):
                acquisition_order.append("competitor")
                competitor_entered.set()
                assert release_competitor.wait(timeout=5)
        except Exception as exc:
            errors.append(exc)
        finally:
            competitor_finished.set()

    # Replace only the adapter's OS view so the Windows path is exercised on
    # every platform while pathlib and real descriptors retain their native OS.
    monkeypatch.setattr(
        run_store_module,
        "os",
        SimpleNamespace(
            **{**vars(os), "name": "nt", "fstat": observed_fstat, "write": checked_write}
        ),
    )
    monkeypatch.setattr(
        run_store_module,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=contention_poll),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    primary = Thread(target=enter_primary, name="empty-lock-primary")
    competitor = Thread(target=enter_competitor, name="empty-lock-competitor")
    primary.start()
    competitor.start()
    try:
        primary.join(timeout=5)
    finally:
        release_competitor.set()
        primary.join(timeout=5)
        competitor.join(timeout=5)

    assert not primary.is_alive()
    assert not competitor.is_alive()
    assert errors == []
    assert empty_observed.is_set()
    assert contended.is_set()
    assert acquisition_order == ["competitor", "primary"]
    assert len(unlocks) == 2
    assert len(set(unlocks)) == 2
    assert writes == []
    assert owner is None
    assert lock_path.stat().st_size == 0
    assert os.path.samestat(original_identity, lock_path.stat())


def test_windows_file_lock_retries_transient_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[int, int, int]] = []

    class FakeMsvcrt:
        LK_NBLCK = 2

        @staticmethod
        def locking(descriptor: int, mode: int, size: int) -> None:
            attempts.append((descriptor, mode, size))
            if len(attempts) < 3:
                raise OSError("lock is busy")

    lock_path = tmp_path / "windows-lock"
    lock_path.write_bytes(b"\0")
    descriptor = os.open(lock_path, os.O_RDWR)
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(run_store_module.time, "sleep", lambda _seconds: None)
    try:
        run_store_module._acquire_windows_file_lock(descriptor)
    finally:
        os.close(descriptor)

    assert len(attempts) == 3
    assert all(mode == FakeMsvcrt.LK_NBLCK and size == 1 for _, mode, size in attempts)


def test_windows_file_lock_timeout_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FakeMsvcrt:
        LK_NBLCK = 2

        @staticmethod
        def locking(_descriptor: int, _mode: int, _size: int) -> None:
            nonlocal attempts
            attempts += 1
            raise OSError("lock is busy")

    lock_path = tmp_path / "windows-lock-timeout"
    lock_path.write_bytes(b"\0")
    descriptor = os.open(lock_path, os.O_RDWR)
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(run_store_module, "_WINDOWS_LOCK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(run_store_module, "_WINDOWS_LOCK_POLL_SECONDS", 0.001)
    started = time.monotonic()
    try:
        with pytest.raises(OSError, match="lock is busy"):
            run_store_module._acquire_windows_file_lock(descriptor)
    finally:
        os.close(descriptor)

    assert attempts >= 2
    assert time.monotonic() - started < 1.0


def test_run_store_diagnostics_have_bounded_operations_and_hashed_identities(
    tmp_path: Path,
    caplog,
) -> None:
    run_id = "run-private-identity"
    paper_id = "paper-private-identity"
    paper_content = "paper content and prompt=must-not-escape"
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    caplog.set_level(logging.INFO, logger=RESEARCH_PERSISTENCE_LOGGER)

    store.save(_record(run_id, paper_id, value=paper_content))
    assert store.get_by_run_id(run_id) == _record(run_id, paper_id, paper_content)
    assert store.get_latest_by_paper_id(paper_id) == _record(
        run_id,
        paper_id,
        paper_content,
    )
    assert store.get_by_run_id("missing-private-run") is None

    records = _research_persistence_records(caplog)
    assert [record.research_event_dimensions["operation"] for record in records] == [
        "run_save",
        "run_get",
        "run_get_latest",
        "run_get",
    ]
    assert [record.research_event_dimensions["outcome"] for record in records] == [
        "succeeded",
        "succeeded",
        "succeeded",
        "not_found",
    ]
    assert (
        records[0].research_event_dimensions["run_identity"]
        == records[1].research_event_dimensions["run_identity"]
    )
    assert (
        records[0].research_event_dimensions["paper_identity"]
        == records[1].research_event_dimensions["paper_identity"]
    )
    assert (
        records[0].research_event_dimensions["run_identity"]
        != records[0].research_event_dimensions["paper_identity"]
    )
    assert records[-1].research_event_dimensions["paper_identity"] == MISSING_IDENTITY_REF
    assert all(
        set(record.research_event_dimensions)
        == {
            "component",
            "operation",
            "outcome",
            "reason",
            "run_identity",
            "paper_identity",
        }
        for record in records
    )
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    for secret in (run_id, paper_id, paper_content, str(tmp_path)):
        assert secret not in caplog.text
        assert all(secret not in repr(record.research_event_dimensions) for record in records)


def test_run_store_failure_diagnostic_uses_typed_reason_without_raw_exception(
    tmp_path: Path,
    caplog,
) -> None:
    raw_exception = f"credential=provider-secret path={tmp_path / 'private' / 'record.json'}"
    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )
    writer.save(_record("run-secret-decoder"))

    def fail_decoder(_value: dict[str, Any]) -> Any:
        raise RuntimeError(raw_exception)

    reader = FilesystemResearchRunStore(tmp_path, result_decoder=fail_decoder)
    caplog.clear()
    caplog.set_level(logging.INFO, logger=RESEARCH_PERSISTENCE_LOGGER)
    with pytest.raises(ResearchRunStoreCorruptionError):
        reader.get_by_run_id("run-secret-decoder")

    records = _research_persistence_records(caplog)
    assert len(records) == 1
    assert records[0].research_event_dimensions["component"] == "run_store"
    assert records[0].research_event_dimensions["operation"] == "run_get"
    assert records[0].research_event_dimensions["outcome"] == "failed"
    assert records[0].research_event_dimensions["reason"] == "content_invalid"
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    assert raw_exception not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_diagnostic_helper_replaces_unknown_values_and_never_captures_exception(
    caplog,
) -> None:
    secret = "credential=top-secret C:/private/paper.pdf"
    caplog.set_level(logging.INFO, logger=RESEARCH_PERSISTENCE_LOGGER)

    try:
        raise RuntimeError(secret)
    except RuntimeError:
        emit_research_persistence_diagnostic(
            component=secret,
            operation=secret,
            outcome=secret,
            reason=secret,
            run_id=secret,
            paper_id=secret,
        )

    record = _research_persistence_records(caplog)[0]
    assert record.research_event_dimensions == {
        "component": SAFE_DIAGNOSTIC_LABEL,
        "operation": SAFE_DIAGNOSTIC_LABEL,
        "outcome": SAFE_DIAGNOSTIC_LABEL,
        "reason": SAFE_DIAGNOSTIC_LABEL,
        "run_identity": record.research_event_dimensions["run_identity"],
        "paper_identity": record.research_event_dimensions["paper_identity"],
    }
    assert record.research_event_dimensions["run_identity"].startswith(
        "redacted:sha256:"
    )
    assert record.research_event_dimensions["paper_identity"].startswith(
        "redacted:sha256:"
    )
    assert (
        record.research_event_dimensions["run_identity"]
        != record.research_event_dimensions["paper_identity"]
    )
    assert record.exc_info is None
    assert record.stack_info is None
    assert secret not in caplog.text


def test_diagnostic_identity_input_is_bounded_before_hashing(caplog) -> None:
    oversized_identity = "x" * 4_097
    caplog.set_level(logging.INFO, logger=RESEARCH_PERSISTENCE_LOGGER)

    emit_research_persistence_diagnostic(
        component="run_store",
        operation="run_get",
        outcome="not_found",
        reason="not_found",
        run_id=oversized_identity,
    )

    record = _research_persistence_records(caplog)[0]
    assert (
        record.research_event_dimensions["run_identity"]
        == SAFE_DIAGNOSTIC_LABEL
    )
    assert oversized_identity not in caplog.text


def test_diagnostic_logging_failure_never_replaces_storage_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_StoredResult.from_dict,
    )

    def fail_log(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("logging backend unavailable")

    monkeypatch.setattr(diagnostics_module._LOGGER, "log", fail_log)
    store.save(_record("run-log-failure"))

    assert store._get_by_run_id("run-log-failure") == _record("run-log-failure")


def test_v2_writer_requires_dual_reader_and_persists_explicit_disposition(
    tmp_path: Path,
) -> None:
    authority_ref = "sha256:" + "a" * 64
    with pytest.raises(ResearchRunStoreValidationError) as incompatible:
        FilesystemResearchRunStore(
            tmp_path / "invalid",
            result_decoder=_identity_decoder,
            write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
            supported_schema_versions=(RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,),
        )
    assert incompatible.value.reason is ResearchRunStoreReason.SCHEMA_UNSUPPORTED

    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )
    store.save(_disposition_record("run-v2", authority_ref=authority_ref))

    record = store.get_by_run_id("run-v2")
    assert record is not None
    assert record.disposition is ResearchRunDisposition.ACCEPTED
    assert record.publication_authority_ref == authority_ref
    state = _read_json(_record_path(store, "run-v2"))
    assert state["schema_version"] == RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2
    assert set(state) == run_store_module._RECORD_V2_FIELDS
    assert state["disposition"] == "accepted"
    index = _read_json(_index_path(store, "paper-disposition"))
    assert index["schema_version"] == RESEARCH_RUN_LATEST_INDEX_SCHEMA_VERSION_V2
    assert set(index) == run_store_module._INDEX_V2_FIELDS
    assert index["run_id"] == "run-v2"


def test_new_quarantine_cannot_shadow_accepted_latest_after_restart(
    tmp_path: Path,
) -> None:
    authority_ref = "sha256:" + "b" * 64
    v1 = FilesystemResearchRunStore(tmp_path, result_decoder=_identity_decoder)
    accepted = _disposition_record("run-accepted")
    v1.save(accepted)
    accepted_bytes = _record_path(v1, accepted.run_id).read_bytes()

    v2 = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )
    halted = _disposition_record(
        "run-halted",
        status="halted",
        quality_passed=False,
        authority_ref=authority_ref,
    )
    v2.save(halted)

    latest = v2.get_latest_by_paper_id("paper-disposition")
    assert latest is not None and latest.run_id == accepted.run_id
    assert [item.run_id for item in v2.list_by_paper_id("paper-disposition")] == [
        accepted.run_id
    ]
    diagnostic = v2.get_by_run_id(halted.run_id)
    assert diagnostic is not None and diagnostic.quarantined
    scope_ref = research_identity_scope_ref(
        {"memory_namespace": "research.public"}
    )
    assert v2.get_diagnostic_by_run_id(
        halted.run_id,
        identity_scope_ref=scope_ref,
    ) == diagnostic
    assert v2.get_diagnostic_by_run_id(
        halted.run_id,
        identity_scope_ref="sha256:" + "c" * 64,
    ) is None
    assert [
        item.run_id
        for item in v2.list_quarantined_by_paper_id(
            "paper-disposition",
            identity_scope_ref=scope_ref,
        )
    ] == [halted.run_id]

    reopened = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
    )
    assert reopened.get_latest_by_paper_id("paper-disposition").run_id == accepted.run_id
    assert _record_path(reopened, accepted.run_id).read_bytes() == accepted_bytes


@pytest.mark.parametrize(
    ("status", "quality_passed"),
    [("halted", False), ("succeeded", False)],
)
def test_failure_only_paper_has_diagnostics_but_no_latest_index(
    tmp_path: Path,
    status: str,
    quality_passed: bool,
) -> None:
    authority_ref = "sha256:" + "d" * 64
    paper_id = f"paper-{status}-{quality_passed}"
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )
    store.save(
        _disposition_record(
            "run-failed",
            paper_id=paper_id,
            status=status,
            quality_passed=quality_passed,
            authority_ref=authority_ref,
        )
    )

    assert store.get_latest_by_paper_id(paper_id) is None
    assert store.list_by_paper_id(paper_id) == []
    assert not _index_path(store, paper_id).exists()
    assert store.get_by_run_id("run-failed").quarantined


def test_concurrent_accepted_and_quarantine_writers_keep_accepted_index(
    tmp_path: Path,
) -> None:
    authority_ref = "sha256:" + "e" * 64
    stores = [
        FilesystemResearchRunStore(
            tmp_path,
            result_decoder=_identity_decoder,
            write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
        )
        for _ in range(2)
    ]
    records = [
        _disposition_record(
            f"run-{index}",
            status="succeeded" if index % 2 == 0 else "halted",
            quality_passed=index % 2 == 0,
            authority_ref=authority_ref,
        )
        for index in range(12)
    ]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(stores[index % 2].save, record)
            for index, record in enumerate(records)
        ]
        for future in futures:
            future.result(timeout=20)

    accepted_run_ids = {
        record.run_id for index, record in enumerate(records) if index % 2 == 0
    }
    latest = stores[0].get_latest_by_paper_id("paper-disposition")
    assert latest is not None and latest.run_id in accepted_run_ids
    assert {
        record.run_id for record in stores[0].list_by_paper_id("paper-disposition")
    } == accepted_run_ids


def test_v1_missing_scope_is_classified_as_quarantine_without_rewriting_bytes(
    tmp_path: Path,
) -> None:
    store = FilesystemResearchRunStore(tmp_path, result_decoder=_identity_decoder)
    result = _disposition_result("run-unscoped")
    result.pop("actor_scope")
    result["trace"].pop("metadata")
    record = ResearchRunRecord(
        run_id="run-unscoped",
        paper_id="paper-disposition",
        result=result,
    )
    store.save(record)
    original = _record_path(store, record.run_id).read_bytes()

    reopened = FilesystemResearchRunStore(tmp_path, result_decoder=_identity_decoder)
    diagnostic = reopened.get_by_run_id(record.run_id)
    assert diagnostic is not None and diagnostic.quarantined
    assert diagnostic.disposition_reason == "identity_scope_missing"
    assert (
        diagnostic.artifact_reference_disposition
        == ResearchRunDispositionReason.LEGACY_QUARANTINED.value
    )
    assert reopened.get_latest_by_paper_id(record.paper_id) is None
    assert _record_path(reopened, record.run_id).read_bytes() == original


def test_v1_single_scope_root_binding_accepts_missing_scope_without_rewrite(
    tmp_path: Path,
) -> None:
    scope_ref = research_identity_scope_ref(
        {"tenant_id": "tenant-a", "memory_namespace": "research:tenant-a"}
    )
    result = _disposition_result("run-root-bound")
    result.pop("actor_scope")
    result["trace"].pop("metadata")
    record = ResearchRunRecord(
        run_id="run-root-bound",
        paper_id="paper-disposition",
        result=result,
    )
    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        legacy_identity_scope_ref=scope_ref,
    )
    writer.save(record)
    original = _record_path(writer, record.run_id).read_bytes()

    unbound = FilesystemResearchRunStore(tmp_path, result_decoder=_identity_decoder)
    unbound_record = unbound.get_by_run_id(record.run_id)
    assert unbound_record is not None and unbound_record.quarantined
    assert unbound.get_latest_by_paper_id(record.paper_id) is None

    rebound = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        legacy_identity_scope_ref=scope_ref,
    )
    rebound_record = rebound.get_by_run_id(record.run_id)
    assert rebound_record is not None and rebound_record.accepted
    assert rebound_record.identity_scope_ref == scope_ref
    assert rebound.get_latest_by_paper_id(record.paper_id).run_id == record.run_id
    assert _record_path(rebound, record.run_id).read_bytes() == original


def test_v1_root_binding_conflict_quarantines_and_removes_legacy_latest(
    tmp_path: Path,
) -> None:
    writer = FilesystemResearchRunStore(tmp_path, result_decoder=_identity_decoder)
    record = _disposition_record(
        "run-scope-conflict",
        scope={"memory_namespace": "research:scope-a"},
    )
    writer.save(record)
    original = _record_path(writer, record.run_id).read_bytes()
    assert writer.get_latest_by_paper_id(record.paper_id) is not None

    conflicting_scope_ref = research_identity_scope_ref(
        {"memory_namespace": "research:scope-b"}
    )
    reopened = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        legacy_identity_scope_ref=conflicting_scope_ref,
    )
    diagnostic = reopened.get_by_run_id(record.run_id)
    assert diagnostic is not None and diagnostic.quarantined
    assert diagnostic.disposition_reason == "identity_scope_conflict"
    assert reopened.get_latest_by_paper_id(record.paper_id) is None
    assert not _index_path(reopened, record.paper_id).exists()
    assert _record_path(reopened, record.run_id).read_bytes() == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("disposition", "unknown"),
        ("disposition_reason", "quality_not_passed"),
        ("identity_scope_ref", "sha256:" + "1" * 64),
        ("subject_scope_ref", "sha256:" + "2" * 64),
        ("publication_authority_ref", "sha256:" + "3" * 64),
        ("artifact_evidence_ref", "sha256:" + "4" * 64),
    ],
)
def test_v2_disposition_metadata_tamper_is_rejected_after_checksum_reseal(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    authority_ref = "sha256:" + "f" * 64
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )
    record = _disposition_record("run-v2-tamper", authority_ref=authority_ref)
    store.save(record)
    path = _record_path(store, record.run_id)
    state = _read_json(path)
    state[field] = value
    state["checksum"] = run_store_module._state_checksum(
        {key: item for key, item in state.items() if key != "checksum"}
    )
    _write_json(path, state)

    with pytest.raises(ResearchRunStoreCorruptionError) as corrupted:
        store.get_by_run_id(record.run_id)
    assert corrupted.value.reason is ResearchRunStoreReason.CONTENT_INVALID


def test_mixed_v1_quarantine_and_v2_accepted_repairs_latest_without_v1_rewrite(
    tmp_path: Path,
) -> None:
    legacy = FilesystemResearchRunStore(tmp_path, result_decoder=_identity_decoder)
    legacy_result = _disposition_result("run-v1-quarantine")
    legacy_result.pop("actor_scope")
    legacy_result["trace"].pop("metadata")
    legacy_record = ResearchRunRecord(
        run_id="run-v1-quarantine",
        paper_id="paper-disposition",
        result=legacy_result,
    )
    legacy.save(legacy_record)
    legacy_bytes = _record_path(legacy, legacy_record.run_id).read_bytes()

    authority_ref = "sha256:" + "9" * 64
    writer = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=_identity_decoder,
        write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )
    accepted = _disposition_record("run-v2-accepted", authority_ref=authority_ref)
    writer.save(accepted)

    reopened = FilesystemResearchRunStore(tmp_path, result_decoder=_identity_decoder)
    latest = reopened.get_latest_by_paper_id("paper-disposition")
    assert latest is not None and latest.run_id == accepted.run_id
    assert [record.run_id for record in reopened.list_by_paper_id("paper-disposition")] == [
        accepted.run_id
    ]
    assert reopened.get_by_run_id(legacy_record.run_id).quarantined
    assert _record_path(reopened, legacy_record.run_id).read_bytes() == legacy_bytes


def _research_persistence_records(caplog):
    return [
        record
        for record in caplog.records
        if getattr(record, "research_event_name", None)
        == RESEARCH_PERSISTENCE_OPERATION_EVENT
    ]
