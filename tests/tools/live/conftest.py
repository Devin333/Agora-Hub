"""Fixtures for live tool tests: real services, throwaway namespaces, cleanup.

Every module here sets ``pytestmark = pytest.mark.tool_live`` (excluded by
default in ``addopts``) and each test names the service it needs with
``@pytest.mark.live_service("...")``; unmarked tests need only the network. A
test whose service is not configured is skipped with the missing variable
named; see ``tests/tools/harness/live.py`` for the variables.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from tests.tools.harness.live import LiveSettings

LIVE_RUN_TAG = f"tool-live-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def live_settings() -> LiveSettings:
    return LiveSettings.from_env()


@pytest.fixture(autouse=True)
def _skip_unconfigured_service(request: pytest.FixtureRequest, live_settings: LiveSettings) -> None:
    marker = request.node.get_closest_marker("live_service")
    services = marker.args if marker is not None else ("network",)
    for service in services:
        reason = live_settings.missing(service)
        if reason is not None:
            pytest.skip(f"{service}: {reason}")


@pytest.fixture
def live_postgres_schema(live_settings: LiveSettings) -> Iterator[tuple[str, str]]:
    """A schema that exists for one test, as ``(dsn, schema)``; dropped afterwards."""

    from infrastructure.storage.postgres.connect import connect_postgres

    dsn = str(live_settings.postgres_dsn)
    schema = f"{LIVE_RUN_TAG}-{uuid.uuid4().hex[:6]}".replace("-", "_")
    with connect_postgres(dsn) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.commit()
    try:
        yield dsn, schema
    finally:
        with connect_postgres(dsn) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            connection.commit()


@pytest.fixture
def live_qdrant_collection(live_settings: LiveSettings) -> Iterator[tuple[object, str]]:
    """``(client, collection)`` for one test; the collection is deleted afterwards."""

    from infrastructure.storage.vector.connection import connect_qdrant

    client = connect_qdrant(live_settings.qdrant_url, api_key=live_settings.qdrant_api_key, timeout=10)
    collection = f"{LIVE_RUN_TAG}-{uuid.uuid4().hex[:6]}"
    try:
        yield client, collection
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
        client.close()
