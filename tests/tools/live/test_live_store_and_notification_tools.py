"""postgres.*, report.publish/search, qdrant.* and notification.* against real services.

These write, so each runs only when its dedicated test target is configured,
and only inside a namespace it creates and removes: a throwaway Postgres
schema, a throwaway Qdrant collection. Email and webhook tests send exactly
one message to the configured test recipient or endpoint.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from framework.tool import ToolStatus
from infrastructure.storage.postgres import PostgresRepository
from infrastructure.storage.postgres.connect import connect_postgres
from infrastructure.storage.vector.embeddings import DeterministicEmbeddingModel
from infrastructure.storage.vector.qdrant_store import QdrantVectorStore
from interfaces.services.report_service import ReportApplicationService
from tests.tools.harness.invoke import assert_message_is_safe, expect_succeeded
from tests.tools.harness.live import LiveSettings
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.test_notification_webhook_tools import outcome

pytestmark = pytest.mark.tool_live

VECTOR_SIZE = 64
REPORT = {"title": "Tool live weekly", "sections": [{"title": "Findings", "content": "Live persistence works."}]}


def _schema_repository(dsn: str, schema: str) -> PostgresRepository:
    # Every connection lands in the throwaway schema; the repository code is unchanged.
    return PostgresRepository(dsn, connection_factory=lambda: connect_postgres(dsn, options=f"-c search_path={schema}"))


def _postgres_env(tool_env_factory, dsn: str, schema: str) -> tuple[ToolEnvironment, PostgresRepository]:
    repository = _schema_repository(dsn, schema)
    repository.migrate()
    environment = tool_env_factory(
        postgres_repository=repository,
        postgres_source_health_repository=repository,
        persistence_repository=repository,
        report_service=ReportApplicationService(repository=repository),
    )
    return environment, repository


def _report(report_id: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "report_id": report_id,
        "run_id": "run-tool-live",
        "status": "completed",
        "title": "Tool live weekly",
        "report_json": {"sections": [{"title": "Findings", "body": "Live persistence works."}]},
        "quality_score": 0.8,
    }
    args.update(overrides)
    return args


@pytest.mark.live_service("postgres")
def test_saved_report_is_found_by_search(tool_env_factory, live_postgres_schema) -> None:
    environment, repository = _postgres_env(tool_env_factory, *live_postgres_schema)

    expect_succeeded(environment.run("postgres.save_report", _report("live-r1")))
    found = expect_succeeded(environment.run("report.search", {"query": "tool live"}))

    assert repository.get_report("live-r1").title == "Tool live weekly"
    assert "live-r1" in str(found)


@pytest.mark.live_service("postgres")
def test_same_report_id_saved_twice_keeps_one_row_with_the_last_values(tool_env_factory, live_postgres_schema) -> None:
    environment, repository = _postgres_env(tool_env_factory, *live_postgres_schema)

    expect_succeeded(environment.run("postgres.save_report", _report("live-r1", title="First draft")))
    expect_succeeded(environment.run("postgres.save_report", _report("live-r1", title="Final")))

    [(count,)] = repository.fetch_all("SELECT count(*) FROM reports WHERE report_id = %s", ("live-r1",))
    assert count == 1
    assert repository.get_report("live-r1").title == "Final"


@pytest.mark.live_service("postgres")
def test_concurrent_saves_of_one_report_converge_on_one_row(tool_env_factory, live_postgres_schema) -> None:
    environment, repository = _postgres_env(tool_env_factory, *live_postgres_schema)
    barrier = threading.Barrier(8)
    runs: list[Any] = [None] * 8

    def worker(index: int) -> None:
        barrier.wait()
        runs[index] = environment.run("postgres.save_report", _report("live-race", title=f"writer {index}"))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert all(run.status is ToolStatus.SUCCEEDED for run in runs), [run.describe() for run in runs if run.status is not ToolStatus.SUCCEEDED][:2]
    [(count,)] = repository.fetch_all("SELECT count(*) FROM reports WHERE report_id = %s", ("live-race",))
    assert count == 1
    assert repository.get_report("live-race").title in {f"writer {index}" for index in range(8)}


@pytest.mark.live_service("postgres")
def test_source_health_upserts_by_source_id(tool_env_factory, live_postgres_schema) -> None:
    environment, repository = _postgres_env(tool_env_factory, *live_postgres_schema)

    for status in ("healthy", "degraded", "down"):
        expect_succeeded(environment.run("postgres.update_source_health", {"source_id": "live-s1", "status": status}))

    health = repository.get_source_health("live-s1")
    assert health is not None and str(getattr(health.status, "value", health.status)) == "down"
    [(count,)] = repository.fetch_all("SELECT count(*) FROM source_health WHERE source_id = %s", ("live-s1",))
    assert count == 1


@pytest.mark.live_service("postgres")
def test_report_publish_round_trips_through_the_real_repository(tool_env_factory, live_postgres_schema) -> None:
    environment, repository = _postgres_env(tool_env_factory, *live_postgres_schema)

    published = expect_succeeded(environment.run("report.publish", {"report": REPORT, "quality_score": 0.9}))

    stored = repository.get_report(published["report_id"])
    assert stored.title == "Tool live weekly"


@pytest.mark.live_service("postgres")
def test_wrong_password_fails_without_leaking_it(tool_env_factory, live_settings: LiveSettings) -> None:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(str(live_settings.postgres_dsn))
    wrong_password = f"wrong-{uuid.uuid4().hex[:8]}"
    netloc = f"{parts.username or 'postgres'}:{wrong_password}@{parts.hostname}:{parts.port or 5432}"
    bad_dsn = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    environment = tool_env_factory(postgres_repository=PostgresRepository(bad_dsn))

    run = environment.run("postgres.save_report", _report("live-never"))

    assert run.status is not ToolStatus.SUCCEEDED
    assert wrong_password not in str(run.observation.result.to_dict())
    assert_message_is_safe(run.error_message, secrets=[wrong_password])


def _qdrant_env(tool_env_factory, client: Any, collection: str) -> ToolEnvironment:
    store = QdrantVectorStore(client, embedding_model=DeterministicEmbeddingModel(dimension=VECTOR_SIZE), vector_size=VECTOR_SIZE)
    return tool_env_factory(qdrant_vector_store=store, qdrant_document_store=store, qdrant_collections=[collection])


def _document(collection: str, document_id: str, text: str) -> dict[str, Any]:
    return {"document_id": document_id, "text": text, "source_type": "paper", "collection": collection}


@pytest.mark.live_service("qdrant")
def test_upserted_documents_are_found_by_search(tool_env_factory, live_qdrant_collection) -> None:
    client, collection = live_qdrant_collection
    environment = _qdrant_env(tool_env_factory, client, collection)
    documents = [
        _document(collection, "d1", "agent memory retrieval for long tasks"),
        _document(collection, "d2", "image segmentation with transformers"),
    ]

    expect_succeeded(environment.run("qdrant.upsert", {"documents": documents}))
    output = expect_succeeded(environment.run("qdrant.search", {"collection": collection, "query": "agent memory retrieval", "limit": 2}))

    assert "d1" in str(output)


@pytest.mark.live_service("qdrant")
def test_reupserting_a_document_replaces_it(tool_env_factory, live_qdrant_collection) -> None:
    client, collection = live_qdrant_collection
    environment = _qdrant_env(tool_env_factory, client, collection)

    expect_succeeded(environment.run("qdrant.upsert", {"documents": [_document(collection, "d1", "first version")]}))
    expect_succeeded(environment.run("qdrant.upsert", {"documents": [_document(collection, "d1", "second version")]}))

    assert client.count(collection).count == 1


@pytest.mark.live_service("qdrant")
def test_collection_outside_the_allowlist_is_refused(tool_env_factory, live_qdrant_collection) -> None:
    client, collection = live_qdrant_collection
    environment = _qdrant_env(tool_env_factory, client, collection)

    run = environment.run("qdrant.search", {"collection": f"{collection}-other", "query": "x"})

    assert run.status is ToolStatus.FAILED
    assert not client.collection_exists(f"{collection}-other")


@pytest.mark.live_service("smtp")
def test_email_reaches_the_test_server(tool_env_factory, live_settings: LiveSettings) -> None:
    target = live_settings.smtp
    assert target is not None
    environment = tool_env_factory(
        notification_options={
            "smtp_host": target.host,
            "smtp_port": target.port,
            "smtp_username": target.username,
            "smtp_password": target.password,
            "smtp_use_tls": target.security == "tls",
            "smtp_use_starttls": target.security == "starttls",
            "email_from_address": target.from_address,
            "allowed_email_domains": target.domains,
            "allowed_private_networks": [],
        }
    )

    run = environment.run("notification.email",
        {"to": [target.to_address], "subject": f"NewsRoom tool live test {uuid.uuid4().hex[:8]}", "text_body": "Sent by tests/tools/live."},
    )

    assert outcome(run) == "applied", run.describe()
    if target.password:
        assert target.password not in str(run.observation.result.to_dict())


@pytest.mark.live_service("smtp")
def test_email_to_a_domain_outside_the_allowlist_is_not_sent(tool_env_factory, live_settings: LiveSettings) -> None:
    target = live_settings.smtp
    assert target is not None
    environment = tool_env_factory(
        notification_options={
            "smtp_host": target.host,
            "smtp_port": target.port,
            "email_from_address": target.from_address,
            "allowed_email_domains": target.domains,
        }
    )

    run = environment.run("notification.email", {"to": ["someone@not-allowed.invalid"], "subject": "never sent", "text_body": "x"})

    assert outcome(run) == "not_applied"


@pytest.mark.live_service("webhook")
def test_webhook_is_delivered_to_the_test_endpoint(tool_env_factory, live_settings: LiveSettings) -> None:
    from urllib.parse import urlsplit

    url = str(live_settings.webhook_url)
    environment = tool_env_factory(
        notification_options={"allowed_webhook_domains": [str(urlsplit(url).hostname)], "allowed_private_networks": []}
    )

    run = environment.run("notification.webhook", {"url": url, "payload": {"event": "tool.live", "id": uuid.uuid4().hex}})

    assert outcome(run) == "applied", run.describe()
    assert 200 <= int(run.output["status_code"]) < 300
