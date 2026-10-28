from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from backend.research.reader_repair import ReaderRepairService
from interfaces.services.reader_repair_memory import (
    PostgresReaderRepairFailureDiagnosticCommitPort,
    PostgresReaderRepairMemoryCommitPort,
)
from interfaces.services import reader_repair_factory
import infrastructure.storage.postgres as postgres_storage


def test_reader_repair_memory_factory_returns_none_without_database_dsn() -> None:
    assert reader_repair_factory.build_reader_repair_memory_from_env(env={}) is None
    assert (
        reader_repair_factory.build_reader_repair_memory_commit_port_from_env(
            env={}
        )
        is None
    )
    assert (
        reader_repair_factory.build_reader_repair_failure_diagnostic_commit_port_from_env(
            env={}
        )
        is None
    )


def test_reader_repair_service_factory_uses_postgres_memory_when_dsn_is_configured(
    monkeypatch,
) -> None:
    created = {}

    class FakeRepository:
        def __init__(self, dsn):
            created["dsn"] = dsn

    monkeypatch.setattr(
        postgres_storage,
        "PostgresReaderRepairMemoryRepository",
        FakeRepository,
    )

    service = reader_repair_factory.build_reader_repair_service_from_env(
        env={"NEWS_DATABASE_DSN": "postgresql://example"}
    )

    assert isinstance(service, ReaderRepairService)
    assert service.memory_service.memory.__class__.__name__ == "PostgresReaderRepairMemoryPort"
    assert created["dsn"] == "postgresql://example"


def test_reader_repair_commit_port_factory_builds_inactive_postgres_adapter(
    monkeypatch,
) -> None:
    created = {}

    class FakeRepository:
        def __init__(self, dsn):
            created["dsn"] = dsn

    monkeypatch.setattr(
        postgres_storage,
        "PostgresReaderRepairMemoryRepository",
        FakeRepository,
    )

    port = reader_repair_factory.build_reader_repair_memory_commit_port_from_env(
        env={"NEWS_DATABASE_DSN": "postgresql://example"}
    )

    assert isinstance(port, PostgresReaderRepairMemoryCommitPort)
    assert created["dsn"] == "postgresql://example"


def test_reader_repair_failure_diagnostic_factory_builds_inactive_postgres_adapter(
    monkeypatch,
) -> None:
    created = {}

    class FakeRepository:
        def __init__(self, dsn):
            created["dsn"] = dsn

    monkeypatch.setattr(
        postgres_storage,
        "PostgresReaderRepairMemoryRepository",
        FakeRepository,
    )

    port = (
        reader_repair_factory.build_reader_repair_failure_diagnostic_commit_port_from_env(
            env={"NEWS_DATABASE_DSN": "postgresql://example"}
        )
    )

    assert isinstance(port, PostgresReaderRepairFailureDiagnosticCommitPort)
    assert created["dsn"] == "postgresql://example"


def test_reader_repair_factory_imports_without_the_postgres_driver() -> None:
    # Deployments without a database (the hosted image) do not install psycopg.
    command = (
        "import sys; sys.modules['psycopg'] = None; "
        "import interfaces.services.reader_repair_factory as factory; "
        "assert factory.build_reader_repair_memory_from_env(env={}) is None"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
