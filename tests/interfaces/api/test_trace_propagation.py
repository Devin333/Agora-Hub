from __future__ import annotations

from fastapi.testclient import TestClient

from framework.events import (
    W3CSpanContext,
    current_trace_context,
    is_valid_span_id,
    is_valid_trace_id,
)
from interfaces.api import create_app


REMOTE_TRACE_ID = "1" * 32
REMOTE_SPAN_ID = "2" * 16


def test_http_inbound_creates_child_and_injects_response_context(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_PRELOAD_RERANKER", "0")
    app, observed = _trace_probe_app()

    with TestClient(app) as client:
        response = client.get(
            "/_trace-probe",
            headers={
                "traceparent": f"00-{REMOTE_TRACE_ID}-{REMOTE_SPAN_ID}-01",
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["trace_id"] == REMOTE_TRACE_ID
    assert payload["parent_span_id"] == REMOTE_SPAN_ID
    assert is_valid_span_id(payload["span_id"])
    assert payload["span_id"] != REMOTE_SPAN_ID
    assert response.headers["traceparent"] == (
        f"00-{REMOTE_TRACE_ID}-{payload['span_id']}-01"
    )
    assert observed == [
        W3CSpanContext(
            trace_id=REMOTE_TRACE_ID,
            span_id=payload["span_id"],
            parent_span_id=REMOTE_SPAN_ID,
            trace_flags="01",
        )
    ]


def test_http_restarts_malformed_or_duplicate_context_without_scope_leak(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NEWS_PRELOAD_RERANKER", "0")
    app, observed = _trace_probe_app()
    second_trace_id = "3" * 32
    second_span_id = "4" * 16

    with TestClient(app) as client:
        malformed = client.get(
            "/_trace-probe",
            headers={
                "traceparent": "malformed",
                "baggage": "run_id=attacker-selected",
            },
        )
        duplicate = client.get(
            "/_trace-probe",
            headers=[
                ("traceparent", f"00-{REMOTE_TRACE_ID}-{REMOTE_SPAN_ID}-01"),
                ("traceparent", f"00-{second_trace_id}-{second_span_id}-01"),
            ],
        )
        fresh = client.get("/_trace-probe")

    payloads = [malformed.json(), duplicate.json(), fresh.json()]
    assert all(response.status_code == 200 for response in (malformed, duplicate, fresh))
    assert all(is_valid_trace_id(payload["trace_id"]) for payload in payloads)
    assert len({payload["trace_id"] for payload in payloads}) == 3
    assert duplicate.json()["trace_id"] not in {REMOTE_TRACE_ID, second_trace_id}
    assert "attacker-selected" not in repr(payloads)
    assert len(observed) == 3


def _trace_probe_app():
    app = create_app(audit_emitter_factory=None)
    observed: list[W3CSpanContext] = []

    @app.get("/_trace-probe")
    async def trace_probe() -> dict[str, str | None]:
        context = current_trace_context()
        assert isinstance(context, W3CSpanContext)
        observed.append(context)
        return {
            "trace_id": context.trace_id,
            "span_id": context.span_id,
            "parent_span_id": context.parent_span_id,
        }

    return app, observed
