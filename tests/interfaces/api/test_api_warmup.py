from __future__ import annotations

from threading import Event

from fastapi.testclient import TestClient

from interfaces.api.app import create_app
from interfaces.api.warmup import BackgroundWarmup


def test_warmup_runs_once_off_the_calling_thread() -> None:
    release = Event()
    calls: list[str] = []

    def task() -> None:
        release.wait(5)
        calls.append("warm")

    warmup = BackgroundWarmup("test", task)
    warmup.start()
    warmup.start()

    # start() returned while the task is still blocked: nothing waits on it.
    assert warmup.started
    assert warmup.wait(timeout=0.05) is False
    release.set()
    assert warmup.wait(timeout=5) is True
    assert calls == ["warm"]


def test_warmup_failure_is_logged_not_raised(caplog) -> None:
    def task() -> None:
        raise RuntimeError("weights unavailable")

    warmup = BackgroundWarmup("test", task)
    warmup.start()

    assert warmup.wait(timeout=5) is True
    assert "test warm-up skipped" in caplog.text


def test_unstarted_warmup_does_not_block_waiters() -> None:
    assert BackgroundWarmup("test", lambda: None).wait(timeout=0) is True


def test_api_serves_requests_while_the_reranker_warmup_is_still_running(monkeypatch) -> None:
    release = Event()
    started = Event()

    def slow_preload() -> None:
        started.set()
        release.wait(10)

    monkeypatch.setenv("NEWS_PRELOAD_RERANKER", "1")
    monkeypatch.setattr("interfaces.services.paper_rag_factory.preload_reranker", slow_preload)
    app = create_app(audit_emitter_factory=None)
    try:
        with TestClient(app) as client:
            assert started.wait(5)
            assert client.get("/health").status_code == 200
            assert app.state.reranker_warmup.wait(timeout=0) is False
    finally:
        release.set()
    assert app.state.reranker_warmup.wait(timeout=5)


def test_reranker_warmup_honours_the_opt_out(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("NEWS_PRELOAD_RERANKER", "0")
    monkeypatch.setattr("interfaces.services.paper_rag_factory.preload_reranker", lambda: calls.append("preload"))
    app = create_app(audit_emitter_factory=None)

    with TestClient(app):
        pass

    assert app.state.reranker_warmup.started is False
    assert calls == []
