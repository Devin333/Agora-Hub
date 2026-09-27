from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from framework.events.errors import EventStoreUnavailableError
from framework.events.ports import EventStorePort
from framework.events.runtime.publisher import EventRuntime
from framework.events.runtime.activities import ActivityRecorder, RecordedActivityStorePort
from framework.events.runtime.history import HistoryVerifier
from framework.events.runtime.replay_engine import (
    DeterministicReplayEngine,
    ReplayCheckpointStorePort,
    ReplayReducerRegistry,
)
from framework.events.runtime.projection_reducer import (
    register_runtime_projection_reducer,
)
from framework.events.schema import (
    EventSchemaCatalog,
    EventSecurityProjector,
    default_event_schema_catalog,
)
from framework.events.telemetry import (
    EventTelemetry,
    TelemetryInstrumentationScope,
    TelemetryResource,
    default_event_telemetry,
)
from framework.shared.time import utc_now
from infrastructure.storage.events.replay_checkpoints import (
    PostgresReplayCheckpointStore,
    SQLiteReplayCheckpointStore,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore
from infrastructure.storage.events.activity_store import SQLiteRecordedActivityStore


DEFAULT_ARTIFACT_ROOT = Path(".newsroom/runs")
LOCAL_EVENT_DATABASE = Path("_records/events.sqlite3")
DEFAULT_REPLAY_RUNTIME_VERSION = "newsroom.event-replay-runtime/v1"
DEFAULT_SCHEMA_CATALOG_VERSION = "newsroom.default-event-schema-catalog/v1"
ACTIVITY_ENCRYPTION_KEY_ENV = "NEWS_ACTIVITY_ENCRYPTION_KEY"


@dataclass(frozen=True, slots=True)
class DurableEventStorage:
    """Production event/replay storage composed over one durable backend."""

    event_store: EventStorePort
    replay_checkpoint_store: ReplayCheckpointStorePort
    event_runtime: EventRuntime
    schema_catalog: EventSchemaCatalog
    activity_store: RecordedActivityStorePort | None = None
    telemetry: EventTelemetry | None = None

    @property
    def activity_recorder(self) -> ActivityRecorder | None:
        return (
            None
            if self.activity_store is None
            else ActivityRecorder(self.activity_store)
        )

    def create_harness_transition_port(
        self,
        *,
        tenant_id: str,
        security_classification: str = "internal",
    ):
        """Compose Harness on the same durable event and secure activity stores."""

        if self.activity_store is None:
            raise EventStoreUnavailableError(
                f"{ACTIVITY_ENCRYPTION_KEY_ENV} is required for durable Harness activities"
            )
        from framework.harness.control_plane.durable_events import (
            DurableHarnessTransitionPort,
            HarnessEventCanonicalAdapter,
        )

        return DurableHarnessTransitionPort(
            self.event_runtime,
            self.event_store,
            secure_activity_store=self.activity_store,
            adapter=HarnessEventCanonicalAdapter(
                tenant_id=tenant_id,
                security_classification=security_classification,
            ),
        )

    def create_replay_engine(
        self,
        *,
        reducers: ReplayReducerRegistry | None = None,
        history_verifier: HistoryVerifier | None = None,
        runtime_version: str = DEFAULT_REPLAY_RUNTIME_VERSION,
        schema_catalog_version: str = DEFAULT_SCHEMA_CATALOG_VERSION,
        clock: Callable[[], datetime] = utc_now,
        page_size: int = 100,
    ) -> DeterministicReplayEngine:
        """Compose replay over the same authoritative event/checkpoint backend."""

        if reducers is None:
            reducers = ReplayReducerRegistry()
            register_runtime_projection_reducer(reducers)
        return DeterministicReplayEngine(
            self.event_store,
            self.schema_catalog,
            reducers,
            self.replay_checkpoint_store,
            runtime_version=runtime_version,
            schema_catalog_version=schema_catalog_version,
            clock=clock,
            page_size=page_size,
            history_verifier=history_verifier,
            telemetry=self.telemetry,
        )


def event_store_from_env(
    *,
    artifact_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> EventStorePort:
    """Compose the canonical durable event store without silent fallback.

    A non-empty ``NEWS_DATABASE_DSN`` selects the shared PostgreSQL adapter.
    Otherwise a file-backed, single-host SQLite store is created below the
    configured artifact root.  Legacy JSONL stores are intentionally absent
    from this production composition boundary and remain explicit migration,
    import, and export adapters only.
    """

    return durable_event_storage_from_env(
        artifact_root=artifact_root,
        env=env,
    ).event_store


def durable_event_storage_from_env(
    *,
    artifact_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> DurableEventStorage:
    """Compose canonical events and replay checkpoints without memory fallback."""

    values = env if env is not None else os.environ
    dsn = str(values.get("NEWS_DATABASE_DSN") or "").strip()
    if dsn:
        try:
            from infrastructure.storage.events.postgres import PostgresDurableEventStore
        except ModuleNotFoundError as exc:
            if not _is_missing_psycopg(exc):
                raise
            raise EventStoreUnavailableError(
                "PostgreSQL durable event storage requires psycopg"
            ) from exc

        event_store = PostgresDurableEventStore(dsn)
        telemetry = _event_runtime_telemetry()
        schema_catalog = default_event_schema_catalog(telemetry=telemetry)
        activity_store = None
        activity_key = str(values.get(ACTIVITY_ENCRYPTION_KEY_ENV) or "").strip()
        if activity_key:
            from infrastructure.storage.events.activity_store import (
                PostgresRecordedActivityStore,
            )

            activity_store = PostgresRecordedActivityStore(
                dsn,
                encryption_key=activity_key,
            )
        event_runtime = EventRuntime(
            store=event_store,
            schema_catalog=schema_catalog,
            security_projector=(
                None
                if activity_store is None
                else EventSecurityProjector(secure_payload_store=activity_store)
            ),
            telemetry=telemetry,
            backend="postgresql",
        )
        return DurableEventStorage(
            event_store=event_store,
            replay_checkpoint_store=PostgresReplayCheckpointStore(dsn),
            event_runtime=event_runtime,
            schema_catalog=schema_catalog,
            activity_store=activity_store,
            telemetry=telemetry,
        )

    configured_root_value = str(values.get("NEWS_ARTIFACT_ROOT") or "").strip()
    configured_root = (
        Path(artifact_root)
        if artifact_root is not None
        else Path(configured_root_value or DEFAULT_ARTIFACT_ROOT)
    )
    database = configured_root / LOCAL_EVENT_DATABASE
    event_store = SQLiteEventStore(database)
    telemetry = _event_runtime_telemetry()
    schema_catalog = default_event_schema_catalog(telemetry=telemetry)
    activity_key = str(values.get(ACTIVITY_ENCRYPTION_KEY_ENV) or "").strip()
    activity_store = (
        None
        if not activity_key
        else SQLiteRecordedActivityStore(
            database,
            encryption_key=activity_key,
        )
    )
    return DurableEventStorage(
        event_store=event_store,
        replay_checkpoint_store=SQLiteReplayCheckpointStore(database),
        event_runtime=EventRuntime(
            store=event_store,
            schema_catalog=schema_catalog,
            security_projector=(
                None
                if activity_store is None
                else EventSecurityProjector(secure_payload_store=activity_store)
            ),
            telemetry=telemetry,
            backend="sqlite",
        ),
        schema_catalog=schema_catalog,
        activity_store=activity_store,
        telemetry=telemetry,
    )


def _event_runtime_telemetry() -> EventTelemetry:
    return default_event_telemetry(
        resource=TelemetryResource(service_name="newsroom-event-runtime"),
        scope=TelemetryInstrumentationScope(
            name="framework.events.runtime",
            version="1",
        ),
    )


def _is_missing_psycopg(exc: ModuleNotFoundError) -> bool:
    missing = str(exc.name or "")
    return (
        missing == "psycopg"
        or missing.startswith("psycopg.")
        or missing == "psycopg_pool"
        or missing.startswith("psycopg_pool.")
    )


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "ACTIVITY_ENCRYPTION_KEY_ENV",
    "DEFAULT_REPLAY_RUNTIME_VERSION",
    "DEFAULT_SCHEMA_CATALOG_VERSION",
    "DurableEventStorage",
    "LOCAL_EVENT_DATABASE",
    "durable_event_storage_from_env",
    "event_store_from_env",
]
