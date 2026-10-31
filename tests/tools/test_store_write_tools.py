"""postgres.* and qdrant.* over fault-injecting stores.

The stores behind these tools are Postgres and Qdrant in production. The
fault stores keep real in-memory state with upsert-by-key semantics, so the
tests can tell a lost write from a duplicate from an overwrite.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from backend.layers.output import postgres_tools
from framework.tool import ToolStatus
from infrastructure.storage.postgres import PostgresRepository
from infrastructure.storage.postgres.connect import PostgresConnectionError
from infrastructure.tools import qdrant_tools
from tests.tools.harness.fault_stores import Pass, Raise, RaiseAfter, Stall
from tests.tools.harness.invoke import expect_succeeded, run_batch, run_tool
from tests.tools.harness.registry import QDRANT_COLLECTIONS, ToolEnvironment
from tests.tools.test_notification_webhook_tools import outcome

Build = Callable[..., ToolEnvironment]
REPORT_TOOLS = ("postgres.save_report", "postgres.insert_report")
COLLECTION = QDRANT_COLLECTIONS[0]


def _report(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "report_id": "report-1",
        "run_id": "run-1",
        "status": "completed",
        "title": "Agent memory weekly",
        "report_json": {"sections": [{"title": "Findings", "body": "..."}]},
        "quality_score": 0.82,
    }
    args.update(overrides)
    return args


@pytest.mark.tool_scenario("postgres.save_report", "A")
@pytest.mark.tool_scenario("postgres.insert_report", "A")
@pytest.mark.parametrize("tool", REPORT_TOOLS)
@pytest.mark.parametrize(
    "overrides",
    [
        {"report_id": ""},
        {"report_json": ["not", "an", "object"]},
        {"report_json": {"score": float("nan")}},
        {"report_json": {"when": object()}},
        {"quality_score": float("nan")},
        {"quality_score": float("inf")},
        {"quality_score": 1.5},
        {"citation_coverage_score": -0.1},
        {"quality_score": "high"},
    ],
    ids=["empty-id", "json-array", "json-nan", "json-object", "score-nan", "score-inf", "score-above-1",
         "coverage-negative", "score-text"],
)
def test_unstorable_reports_never_reach_the_database(tool_env: ToolEnvironment, tool: str, overrides: dict) -> None:
    run = run_tool(tool_env.registry, tool, _report(**overrides))

    assert outcome(run) == "not_applied", run.describe()
    assert tool_env.reports.plan.calls("save_report") == 0


@pytest.mark.tool_scenario("postgres.update_source_health", "A")
@pytest.mark.parametrize(
    "args",
    [
        {"source_id": "s1", "status": "on-fire"},
        {"source_id": "", "status": "healthy"},
        {"source_id": "s1", "status": "healthy", "last_success_at": "yesterday"},
        {"source_id": "s1", "status": "healthy", "avg_latency_ms_24h": float("nan")},
        {"source_id": "s1", "status": "healthy", "avg_latency_ms_24h": -5},
        {"source_id": "s1", "status": "down", "last_error": {"error_type": "x"}},
    ],
    ids=["bad-status", "empty-id", "bad-date", "nan-latency", "negative-latency", "incomplete-error"],
)
def test_unstorable_health_updates_never_reach_the_database(tool_env: ToolEnvironment, args: dict) -> None:
    run = run_tool(tool_env.registry, "postgres.update_source_health", args)

    assert outcome(run) == "not_applied", run.describe()
    assert tool_env.reports.plan.calls("update_source_health") == 0


@pytest.mark.tool_scenario("postgres.save_report", "B")
@pytest.mark.tool_scenario("postgres.insert_report", "B")
@pytest.mark.tool_scenario("postgres.update_source_health", "B")
@pytest.mark.parametrize(
    ("tool", "method", "args"),
    [
        ("postgres.save_report", "save_report", _report()),
        ("postgres.insert_report", "save_report", _report()),
        ("postgres.update_source_health", "update_source_health", {"source_id": "s1", "status": "healthy"}),
    ],
)
@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (Raise(PostgresConnectionError("could not connect to Postgres: connection refused")), "not_applied"),
        (Raise(OSError("server closed the connection unexpectedly")), "indeterminate"),
    ],
    ids=["never-connected", "dropped-mid-write"],
)
def test_database_failures_say_whether_the_write_can_have_happened(
    tool_env: ToolEnvironment, tool: str, method: str, args: dict, fault: Any, expected: str
) -> None:
    tool_env.reports.plan.script(method, fault)

    run = run_tool(tool_env.registry, tool, args)

    assert outcome(run) == expected, run.describe()


@pytest.mark.tool_scenario("postgres.save_report", "C")
@pytest.mark.tool_scenario("postgres.insert_report", "C")
@pytest.mark.tool_scenario("postgres.update_source_health", "C")
@pytest.mark.parametrize(
    ("tool", "method", "args"),
    [
        ("postgres.save_report", "save_report", _report()),
        ("postgres.insert_report", "save_report", _report()),
        ("postgres.update_source_health", "update_source_health", {"source_id": "s1", "status": "healthy"}),
    ],
)
def test_stalled_database_ends_on_the_tool_timeout(
    tool_env_factory: Build, monkeypatch: pytest.MonkeyPatch, tool: str, method: str, args: dict
) -> None:
    monkeypatch.setattr(postgres_tools, "POSTGRES_TOOL_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.reports.plan.script(method, Stall())
    try:
        run = run_tool(environment.registry, tool, args)
        assert run.status is ToolStatus.TIMEOUT, run.describe()
        assert outcome(run) == "indeterminate"
        assert run.elapsed_seconds < 1.2
    finally:
        environment.reports.plan.release()


@pytest.mark.tool_scenario("postgres.save_report", "F")
@pytest.mark.tool_scenario("postgres.insert_report", "F")
@pytest.mark.parametrize("tool", REPORT_TOOLS)
def test_retrying_after_a_lost_acknowledgement_overwrites_not_duplicates(tool_env: ToolEnvironment, tool: str) -> None:
    tool_env.reports.plan.script("save_report", RaiseAfter(TimeoutError("ack lost")), Pass())

    first = run_tool(tool_env.registry, tool, _report())
    assert outcome(first) == "indeterminate"
    expect_succeeded(run_tool(tool_env.registry, tool, _report(title="Agent memory weekly v2")))

    assert list(tool_env.reports.reports) == ["report-1"]
    assert tool_env.reports.reports["report-1"].title == "Agent memory weekly v2"


@pytest.mark.tool_scenario("postgres.update_source_health", "F")
def test_health_update_is_keyed_by_source(tool_env: ToolEnvironment) -> None:
    for status in ("healthy", "degraded", "down"):
        expect_succeeded(run_tool(tool_env.registry, "postgres.update_source_health", {"source_id": "s1", "status": status}))

    assert list(tool_env.reports.source_health) == ["s1"]
    assert tool_env.reports.source_health["s1"].status.value == "down"


@pytest.mark.tool_scenario("postgres.save_report", "G")
@pytest.mark.tool_scenario("postgres.insert_report", "G")
@pytest.mark.tool_scenario("postgres.update_source_health", "G")
@pytest.mark.parametrize(
    "dsn",
    ["postgresql://reporter:hunter2-SECRET@[unterminated", "postgresql://reporter:hunter2-SECRET@127.0.0.1:9/news?connect_timeout=1"],
    ids=["malformed", "unreachable"],
)
def test_database_password_never_reaches_the_model(tool_env_factory: Build, dsn: str) -> None:
    repository = PostgresRepository(dsn)
    environment = tool_env_factory(postgres_repository=repository, postgres_source_health_repository=repository)

    for tool, args in (
        ("postgres.save_report", _report()),
        ("postgres.update_source_health", {"source_id": "s1", "status": "healthy"}),
    ):
        run = run_tool(environment.registry, tool, args)
        assert outcome(run) == "not_applied", run.describe()
        assert "hunter2" not in run.error_message


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> "_RecordingConnection":
        return self

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((sql, params))

    def commit(self) -> None:
        return None


@pytest.mark.tool_scenario("postgres.save_report", "G")
@pytest.mark.tool_scenario("postgres.insert_report", "G")
@pytest.mark.parametrize("tool", REPORT_TOOLS)
def test_model_text_is_bound_as_parameters_never_spliced_into_sql(tool_env_factory: Build, tool: str) -> None:
    connection = _RecordingConnection()
    repository = PostgresRepository("postgresql://unused", connection_factory=lambda: connection)
    environment = tool_env_factory(postgres_repository=repository)
    hostile = "r1'); DROP TABLE reports; --"

    expect_succeeded(run_tool(environment.registry, tool, _report(report_id=hostile, title=hostile)))

    [(sql, params)] = connection.statements
    assert "DROP TABLE" not in sql
    assert hostile in params


def _search(environment: ToolEnvironment, **args: Any):
    return run_tool(environment.registry, "qdrant.search", {"collection": COLLECTION, "query": "agent memory", **args})


def _document(document_id: str = "doc-1", **overrides: Any) -> dict[str, Any]:
    document = {"document_id": document_id, "text": "Agent memory notes", "source_type": "paper", "collection": COLLECTION}
    document.update(overrides)
    return document


@pytest.mark.tool_scenario("qdrant.search", "A")
@pytest.mark.parametrize(
    "args",
    [
        {"collection": ""},
        {"query": "", "vector": None},
        {"vector": "not-a-list"},
        {"vector": []},
        {"vector": [0.1, float("nan")]},
        {"vector": [0.1, float("inf")]},
        {"filters": ["not", "an", "object"]},
        {"limit": "many"},
    ],
    ids=["empty-collection", "no-query", "vector-string", "vector-empty", "vector-nan", "vector-inf",
         "filters-array", "limit-text"],
)
def test_unusable_searches_never_reach_the_store(tool_env: ToolEnvironment, args: dict) -> None:
    arguments = {"collection": COLLECTION, "query": "agent memory", **args}
    arguments = {key: value for key, value in arguments.items() if value is not None}

    run = run_tool(tool_env.registry, "qdrant.search", arguments)

    assert run.status is ToolStatus.FAILED, run.describe()
    assert tool_env.vectors.plan.calls("search") == 0


@pytest.mark.tool_scenario("qdrant.search", "B")
def test_store_failure_is_a_clean_read_failure(tool_env: ToolEnvironment) -> None:
    tool_env.vectors.plan.script("search", Raise(ConnectionError("qdrant unavailable")))

    run = _search(tool_env)

    assert run.status is ToolStatus.FAILED
    assert run.observation.result.indeterminate is False
    assert "qdrant unavailable" in run.error_message


@pytest.mark.tool_scenario("qdrant.search", "C")
def test_stalled_search_ends_on_the_tool_timeout(tool_env_factory: Build, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qdrant_tools, "QDRANT_TOOL_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.vectors.plan.script("search", Stall())
    try:
        run = _search(environment)
        assert run.status is ToolStatus.TIMEOUT, run.describe()
        assert run.elapsed_seconds < 1.2
    finally:
        environment.vectors.plan.release()


@pytest.mark.tool_scenario("qdrant.search", "E")
def test_parallel_searches_overlap(tool_env: ToolEnvironment) -> None:
    tool_env.vectors.plan.script("search", Stall(seconds=0.2))

    observations, elapsed = run_batch(
        tool_env.registry,
        [("qdrant.search", {"collection": COLLECTION, "query": f"topic {i}"}) for i in range(12)],
    )

    assert all(observation.status is ToolStatus.SUCCEEDED for observation in observations)
    assert elapsed < 1.5  # serial would be 2.4s


@pytest.mark.tool_scenario("qdrant.search", "G")
@pytest.mark.tool_scenario("qdrant.upsert", "G")
@pytest.mark.parametrize("collection", ["framework_memory", "other_tenant_documents"])
def test_collections_outside_the_allowlist_are_refused(tool_env: ToolEnvironment, collection: str) -> None:
    search = _search(tool_env, collection=collection)
    upsert = run_tool(tool_env.registry, "qdrant.upsert", {"documents": [_document(collection=collection)]})

    assert search.status is ToolStatus.FAILED and "not allowed" in search.error_message
    assert outcome(upsert) == "not_applied"
    assert tool_env.vectors.plan.calls("search") == tool_env.vectors.plan.calls("upsert_documents") == 0


@pytest.mark.tool_scenario("qdrant.search", "G")
@pytest.mark.tool_scenario("qdrant.upsert", "G")
def test_without_configured_collections_qdrant_tools_refuse_everything(tool_env_factory: Build) -> None:
    environment = tool_env_factory(qdrant_collections=[])

    assert "no allowed collections" in _search(environment).error_message
    upsert = run_tool(environment.registry, "qdrant.upsert", {"documents": [_document()]})
    assert outcome(upsert) == "not_applied"


@pytest.mark.tool_scenario("qdrant.upsert", "A")
@pytest.mark.parametrize(
    "documents",
    [
        [],
        "not-a-list",
        [_document(document_id=f"doc-{i}") for i in range(101)],
        [{"text": "missing id", "source_type": "paper", "collection": COLLECTION}],
        [_document(vector=[0.1, float("nan")])],
        [_document(payload="not-an-object")],
        ["not-an-object"],
    ],
    ids=["empty", "string", "over-limit", "missing-id", "nan-vector", "payload-string", "item-string"],
)
def test_unusable_upserts_never_reach_the_store(tool_env: ToolEnvironment, documents: Any) -> None:
    run = run_tool(tool_env.registry, "qdrant.upsert", {"documents": documents})

    assert outcome(run) == "not_applied", run.describe()
    assert tool_env.vectors.plan.calls("upsert_documents") == 0


@pytest.mark.tool_scenario("qdrant.upsert", "B")
def test_store_failure_during_upsert_is_indeterminate(tool_env: ToolEnvironment) -> None:
    tool_env.vectors.plan.script("upsert_documents", Raise(ConnectionError("qdrant dropped the request")))
    run = run_tool(tool_env.registry, "qdrant.upsert", {"documents": [_document()]})
    assert outcome(run) == "indeterminate"


@pytest.mark.tool_scenario("qdrant.upsert", "C")
def test_stalled_upsert_ends_on_the_tool_timeout(tool_env_factory: Build, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qdrant_tools, "QDRANT_TOOL_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.vectors.plan.script("upsert_documents", Stall())
    try:
        run = run_tool(environment.registry, "qdrant.upsert", {"documents": [_document()]})
        assert run.status is ToolStatus.TIMEOUT, run.describe()
        assert outcome(run) == "indeterminate"
    finally:
        environment.vectors.plan.release()


@pytest.mark.tool_scenario("qdrant.upsert", "F")
def test_upsert_retry_after_lost_acknowledgement_replaces_by_document_id(tool_env: ToolEnvironment) -> None:
    tool_env.vectors.plan.script("upsert_documents", RaiseAfter(TimeoutError("ack lost")), Pass())

    first = run_tool(tool_env.registry, "qdrant.upsert", {"documents": [_document(text="first draft")]})
    assert outcome(first) == "indeterminate"
    expect_succeeded(run_tool(tool_env.registry, "qdrant.upsert", {"documents": [_document(text="final text")]}))

    hit = tool_env.vectors.inner.get_document(COLLECTION, "doc-1")
    assert hit is not None and hit.text == "final text"
    assert len(tool_env.vectors.inner.list_payloads(COLLECTION)) == 1
