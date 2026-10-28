from __future__ import annotations

import contextvars
import json
import threading
from pathlib import Path

import pytest

from interfaces.services.research_desktop_background import (
    ANALYSIS_WORKERS_ENV,
    ResearchDesktopBackgroundExecutor,
    ResearchDesktopExecutorUnavailableError,
    analysis_worker_count_from_env,
)
from interfaces.services.research_desktop_control import (
    LocalJsonResearchDesktopRequestStore,
    canonical_request_fingerprint,
)
from interfaces.services.research_desktop_service import (
    ResearchDesktopApplicationService,
    ResearchDesktopOwner,
    ResearchDesktopServiceError,
)
from interfaces.services.research_import_service import ResearchImportService
from interfaces.services.research_service import ResearchServiceError
from tests.fixtures.desktop_executor import ManualDesktopExecutor
from tests.interfaces.services.test_research_desktop_operations import _ResearchFacade

OWNER = ResearchDesktopOwner(user_id="alice")
REVISION = "sha256:" + "a" * 64
_REQUEST_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "test_request_scope", default=None
)


class _RunTrackingResearch(_ResearchFacade):
    """Projects a run only after it was executed, like the real run store."""

    def get_run_projection(self, run_id, *, actor):
        if not any(call.run_id == run_id for call in self.analysis_calls):
            raise ResearchServiceError("research_run_not_found", "Research run was not found", status_code=404)
        return super().get_run_projection(run_id, actor=actor)


def _semantic() -> dict[str, object]:
    return {
        "kind": "analyze",
        "paperId": "paper-1",
        "sourceRevision": REVISION,
        "sourceFingerprint": REVISION,
        "sourceUrl": "https://example.test/paper.pdf",
        "pdfUrl": None,
        "objective": None,
        "constraints": [],
    }


def _submit(service: ResearchDesktopApplicationService, request_id: str, generation: str):
    semantic = _semantic()
    return service.submit_analysis(
        OWNER,
        request_id=request_id,
        generation=generation,
        request_fingerprint=canonical_request_fingerprint(semantic),
        paper_id="paper-1",
        source_revision=REVISION,
        source_fingerprint=REVISION,
        source_url=str(semantic["sourceUrl"]),
        constraints=(),
    )


def _service(tmp_path: Path, research: _ResearchFacade | None = None):
    executor = ManualDesktopExecutor()
    store = LocalJsonResearchDesktopRequestStore(tmp_path / "control.json")
    facade = research or _RunTrackingResearch()
    service = ResearchDesktopApplicationService(
        research=facade,
        requests=store,
        imports=ResearchImportService(tmp_path / "imports"),
        executor=executor,
    )
    return service, facade, store, executor


def test_submission_returns_waiting_and_schedules_exactly_one_execution(tmp_path: Path) -> None:
    service, research, _store, executor = _service(tmp_path)
    generation = service.generation()

    first = _submit(service, "analyze-async-1", generation)
    duplicate = _submit(service, "analyze-async-1", generation)
    pending = service.get_operation(OWNER, request_id="analyze-async-1", generation=generation)

    assert first["status"] == duplicate["status"] == pending["status"] == "waiting"
    assert first["runId"] == duplicate["runId"]
    assert executor.pending == 1
    assert research.analysis_calls == []

    executor.drain()
    finished = service.get_operation(OWNER, request_id="analyze-async-1", generation=generation)
    assert finished["status"] == "passed"
    assert finished["runId"] == first["runId"]
    assert len(research.analysis_calls) == 1


def test_running_analysis_is_reported_as_running_until_the_job_finishes(tmp_path: Path) -> None:
    observed: list[str] = []

    class _ObservingResearch(_RunTrackingResearch):
        def analyze_paper(self, command):
            observed.append(
                service.get_operation(
                    OWNER, request_id="analyze-async-2", generation=generation
                )["status"]
            )
            return super().analyze_paper(command)

    service, _research, _store, executor = _service(tmp_path, _ObservingResearch())
    generation = service.generation()
    _submit(service, "analyze-async-2", generation)
    executor.drain()

    assert observed == ["running"]
    assert service.get_operation(
        OWNER, request_id="analyze-async-2", generation=generation
    )["status"] == "passed"


def test_background_failure_is_recorded_on_the_operation(tmp_path: Path) -> None:
    class _FailingResearch(_RunTrackingResearch):
        def analyze_paper(self, command):
            raise ResearchServiceError(
                "research_run_failed", "model provider timed out", status_code=502, retryable=True
            )

    service, research, _store, executor = _service(tmp_path, _FailingResearch())
    generation = service.generation()
    _submit(service, "analyze-async-3", generation)
    executor.drain()

    failed = service.get_operation(OWNER, request_id="analyze-async-3", generation=generation)
    assert failed["status"] == "failed"
    assert failed["errorCode"] == "research_run_failed"
    assert research.analysis_calls == []


def test_quality_rejection_from_background_job_is_projected_as_rejected(tmp_path: Path) -> None:
    class _RejectedResearch(_RunTrackingResearch):
        def get_run_projection(self, run_id, *, actor):
            return {
                **super().get_run_projection(run_id, actor=actor),
                "accepted": False,
                "qualityPassed": False,
                "eligibleForAcceptedExport": False,
            }

    service, _research, _store, executor = _service(tmp_path, _RejectedResearch())
    generation = service.generation()
    _submit(service, "analyze-async-4", generation)
    executor.drain()

    rejected = service.get_operation(OWNER, request_id="analyze-async-4", generation=generation)
    assert rejected["status"] == "rejected"
    assert rejected["errorCode"] == "quality_gate_failed"


def test_background_job_runs_in_the_submitting_request_context(tmp_path: Path) -> None:
    seen: list[str | None] = []
    analyzed = threading.Event()

    class _ContextResearch(_RunTrackingResearch):
        def analyze_paper(self, command):
            seen.append(_REQUEST_SCOPE.get())
            try:
                return super().analyze_paper(command)
            finally:
                analyzed.set()

    service, _research, _store, _manual = _service(tmp_path, _ContextResearch())
    executor = ResearchDesktopBackgroundExecutor(max_workers=1)
    service._executor = executor
    generation = service.generation()
    token = _REQUEST_SCOPE.set("alice-request")
    try:
        _submit(service, "analyze-async-5", generation)
    finally:
        _REQUEST_SCOPE.reset(token)
    assert analyzed.wait(10)
    # Shutdown joins the worker, so the job's terminal write has landed.
    executor.shutdown(timeout=10)

    assert seen == ["alice-request"]
    assert service.get_operation(
        OWNER, request_id="analyze-async-5", generation=generation
    )["status"] == "passed"


def test_abandoned_queued_analysis_fails_instead_of_waiting_forever(tmp_path: Path) -> None:
    service, research, _store, executor = _service(tmp_path)
    generation = service.generation()
    _submit(service, "analyze-async-6", generation)

    assert executor.abandon() == 1
    abandoned = service.get_operation(OWNER, request_id="analyze-async-6", generation=generation)
    assert abandoned["status"] == "failed"
    assert abandoned["errorCode"] == "desktop_execution_abandoned"
    assert research.analysis_calls == []


def test_queued_analysis_of_a_dead_process_needs_review(tmp_path: Path) -> None:
    service, research, store, executor = _service(tmp_path)
    generation = service.generation()
    _submit(service, "analyze-async-7", generation)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    for lease in payload["activeLeases"].values():
        lease["pid"] = 0
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    interrupted = service.get_operation(OWNER, request_id="analyze-async-7", generation=generation)
    assert interrupted["status"] == "needs_action"
    assert interrupted["errorCode"] == "desktop_execution_interrupted"

    # A job that outlived its lease must not execute the interrupted admission.
    executor.drain()
    assert research.analysis_calls == []


def test_stopping_executor_refuses_new_work_with_a_retryable_error(tmp_path: Path) -> None:
    service, _research, _store, _manual = _service(tmp_path)
    executor = ResearchDesktopBackgroundExecutor(max_workers=1)
    executor.shutdown()
    service._executor = executor
    generation = service.generation()

    with pytest.raises(ResearchDesktopServiceError) as refused:
        _submit(service, "analyze-async-8", generation)
    assert refused.value.code == "research_executor_unavailable"
    assert refused.value.status_code == 503
    assert service.get_operation(
        OWNER, request_id="analyze-async-8", generation=generation
    )["status"] == "failed"


def test_executor_shutdown_abandons_queued_tasks_and_joins_workers() -> None:
    executor = ResearchDesktopBackgroundExecutor(max_workers=1)
    release = threading.Event()
    started = threading.Event()
    ran: list[str] = []
    abandoned: list[str] = []

    def blocking() -> None:
        started.set()
        release.wait(10)
        ran.append("blocking")

    executor.submit(blocking, on_abandon=lambda: abandoned.append("blocking"))
    assert started.wait(10)
    executor.submit(lambda: ran.append("queued"), on_abandon=lambda: abandoned.append("queued"))
    stopper = threading.Thread(target=executor.shutdown, kwargs={"timeout": 10})
    stopper.start()
    release.set()
    stopper.join(10)

    assert ran == ["blocking"]
    assert abandoned == ["queued"]
    with pytest.raises(ResearchDesktopExecutorUnavailableError):
        executor.submit(lambda: None)


def test_worker_count_is_configurable_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ANALYSIS_WORKERS_ENV, raising=False)
    assert analysis_worker_count_from_env() == 2
    monkeypatch.setenv(ANALYSIS_WORKERS_ENV, "4")
    assert analysis_worker_count_from_env() == 4
    for invalid in ("0", "33", "many"):
        monkeypatch.setenv(ANALYSIS_WORKERS_ENV, invalid)
        with pytest.raises(ValueError):
            analysis_worker_count_from_env()
