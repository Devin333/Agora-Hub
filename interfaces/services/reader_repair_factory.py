from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from backend.research.reader_repair import ReaderRepairService

# The Postgres-backed ports need psycopg, an optional dependency; they are
# imported only once a database is configured.
if TYPE_CHECKING:
    from infrastructure.storage.postgres import PostgresReaderRepairMemoryRepository
    from interfaces.services.reader_repair_memory import (
        PostgresReaderRepairFailureDiagnosticCommitPort,
        PostgresReaderRepairMemoryCommitPort,
        PostgresReaderRepairMemoryPort,
    )


def _repository(values: Mapping[str, str]) -> PostgresReaderRepairMemoryRepository | None:
    dsn = values.get("NEWS_DATABASE_DSN")
    if not dsn:
        return None
    from infrastructure.storage.postgres import PostgresReaderRepairMemoryRepository
    from infrastructure.storage.postgres.dsn import normalize_dsn

    return PostgresReaderRepairMemoryRepository(normalize_dsn(dsn))


def build_reader_repair_memory_from_env(
    env: Mapping[str, str] | None = None,
) -> PostgresReaderRepairMemoryPort | None:
    repository = _repository(os.environ if env is None else env)
    if repository is None:
        return None
    from interfaces.services.reader_repair_memory import PostgresReaderRepairMemoryPort

    return PostgresReaderRepairMemoryPort(repository)


def build_reader_repair_memory_commit_port_from_env(
    env: Mapping[str, str] | None = None,
) -> PostgresReaderRepairMemoryCommitPort | None:
    repository = _repository(os.environ if env is None else env)
    if repository is None:
        return None
    from interfaces.services.reader_repair_memory import PostgresReaderRepairMemoryCommitPort

    return PostgresReaderRepairMemoryCommitPort(repository)


def build_reader_repair_failure_diagnostic_commit_port_from_env(
    env: Mapping[str, str] | None = None,
) -> PostgresReaderRepairFailureDiagnosticCommitPort | None:
    repository = _repository(os.environ if env is None else env)
    if repository is None:
        return None
    from interfaces.services.reader_repair_memory import PostgresReaderRepairFailureDiagnosticCommitPort

    return PostgresReaderRepairFailureDiagnosticCommitPort(repository)


def build_reader_repair_service_from_env(
    env: Mapping[str, str] | None = None,
) -> ReaderRepairService:
    memory = build_reader_repair_memory_from_env(env)
    if memory is None:
        return ReaderRepairService()
    return ReaderRepairService(memory=memory)


__all__ = [
    "build_reader_repair_failure_diagnostic_commit_port_from_env",
    "build_reader_repair_memory_commit_port_from_env",
    "build_reader_repair_memory_from_env",
    "build_reader_repair_service_from_env",
]
