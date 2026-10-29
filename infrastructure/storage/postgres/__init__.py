"""PostgreSQL persistence package with lazy optional adapter imports."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from infrastructure.storage.postgres.migrations import load_migration_sql
from infrastructure.storage.records import ReportDetailRecord, ReportSummaryRecord

if TYPE_CHECKING:
    from infrastructure.storage.postgres.artifact_index import PostgresArtifactIndexStore
    from infrastructure.storage.postgres.conversation import PostgresConversationStore
    from infrastructure.storage.postgres.lineage import PostgresLineageStore
    from infrastructure.storage.postgres.memory_repository import (
        PostgresIntelligenceMemoryRepository,
    )
    from infrastructure.storage.postgres.metrics import PostgresStorageMetricsCollector
    from infrastructure.storage.postgres.repair_memory_repository import (
        PostgresReaderRepairMemoryRepository,
    )
    from infrastructure.storage.postgres.repository import PostgresRepository


_LAZY_ADAPTERS = {
    "PostgresArtifactIndexStore": (
        "infrastructure.storage.postgres.artifact_index",
        "PostgresArtifactIndexStore",
    ),
    "PostgresConversationStore": (
        "infrastructure.storage.postgres.conversation",
        "PostgresConversationStore",
    ),
    "PostgresLineageStore": (
        "infrastructure.storage.postgres.lineage",
        "PostgresLineageStore",
    ),
    "PostgresIntelligenceMemoryRepository": (
        "infrastructure.storage.postgres.memory_repository",
        "PostgresIntelligenceMemoryRepository",
    ),
    "PostgresStorageMetricsCollector": (
        "infrastructure.storage.postgres.metrics",
        "PostgresStorageMetricsCollector",
    ),
    "PostgresReaderRepairMemoryRepository": (
        "infrastructure.storage.postgres.repair_memory_repository",
        "PostgresReaderRepairMemoryRepository",
    ),
    "PostgresRepository": (
        "infrastructure.storage.postgres.repository",
        "PostgresRepository",
    ),
}


def __getattr__(name: str) -> Any:
    """Load a PostgreSQL adapter only when the caller requests that symbol."""

    try:
        module_name, attribute_name = _LAZY_ADAPTERS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "ReportDetailRecord",
    "ReportSummaryRecord",
    "PostgresArtifactIndexStore",
    "PostgresConversationStore",
    "PostgresLineageStore",
    "PostgresIntelligenceMemoryRepository",
    "PostgresReaderRepairMemoryRepository",
    "PostgresStorageMetricsCollector",
    "PostgresRepository",
    "load_migration_sql",
]
