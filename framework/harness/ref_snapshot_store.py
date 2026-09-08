"""Reference grants backed by the canonical run stream and immutable artifacts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any

from framework.agent.artifacts.models import ArtifactRef, ArtifactWriteRequest
from framework.events.canonical import BusinessContext, ProducerIdentity, StoredEvent, canonical_json_bytes, checksum_for, thaw_canonical_json
from framework.events.errors import EventIdentityCollisionError, EventStreamVersionConflictError
from framework.events.ports import EventReaderPort, EventRuntimePort
from framework.events.runtime.models import StreamReadRequest
from framework.events.runtime.publisher import EventPublishRequest
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.task_plan.canonical import checksum, identifier, positive_int
from framework.harness.task_plan.durable_store import TaskPlanArtifactStorePort
from framework.shared.time import utc_now


REF_SNAPSHOT_EVENT_TYPE = "harness_ref_authority_committed"
REF_SNAPSHOT_EVENT_SCHEMA = "newsroom.harness-ref-authority-committed/v1"
REF_SNAPSHOT_EVENT_SOURCE = "framework.harness.ref_authority"
_ARTIFACT_TYPE = "harness.ref-authority.snapshot"
_MAX_SNAPSHOT_BYTES = 1024 * 1024
_MAX_AUTHORITY_GRAPH_SNAPSHOTS = 1024


def _error(message: str, code: str = "REF_SNAPSHOT_CORRUPT") -> HarnessValidationError:
    return HarnessValidationError(message, code=code)


class DurableRefAuthoritySnapshotStore:
    """Only committed events expose grants; orphan files never grant access."""

    is_durable = True

    def __init__(
        self,
        runtime: EventRuntimePort,
        reader: EventReaderPort,
        *,
        artifact_store: TaskPlanArtifactStorePort,
        tenant_id: str | None = None,
        clock: Callable[[], Any] = utc_now,
    ) -> None:
        if not isinstance(runtime, EventRuntimePort) or not isinstance(reader, EventReaderPort):
            raise TypeError("reference snapshots require canonical event runtime and reader ports")
        if not isinstance(artifact_store, TaskPlanArtifactStorePort):
            raise TypeError("artifact_store must implement TaskPlanArtifactStorePort")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if tenant_id is not None and (not isinstance(tenant_id, str) or not tenant_id or tenant_id != tenant_id.strip()):
            raise ValueError("tenant_id must be a nonblank trimmed string")
        self._runtime = runtime
        self._reader = reader
        self._artifacts = artifact_store
        self._tenant_id = tenant_id
        self._clock = clock

    def commit(self, snapshot: RefAuthoritySnapshot) -> str:
        if not isinstance(snapshot, RefAuthoritySnapshot):
            raise TypeError("snapshot must be RefAuthoritySnapshot")
        if snapshot.parent_snapshot_ref is not None:
            snapshot.validate_parent(self.get(run_id=snapshot.run_id, snapshot_ref=snapshot.parent_snapshot_ref))
        snapshot.validate_dependency_sources({
            ref: self.get(run_id=snapshot.run_id, snapshot_ref=ref)
            for ref in {item.source_snapshot_ref for item in snapshot.dependency_bindings}
        })
        content = canonical_json_bytes(snapshot.to_dict())
        if len(content) > _MAX_SNAPSHOT_BYTES:
            raise _error("reference snapshot exceeds its bounded size", "REF_SNAPSHOT_SIZE_EXCEEDED")
        artifact = self._artifact_ref(snapshot.run_id, snapshot.snapshot_ref, checksum_for_bytes(content), len(content))
        for _ in range(8):
            events, high = self._read_events(snapshot.run_id)
            existing = self._find_event(events, "binding_key", snapshot.binding_key)
            if existing is not None:
                recorded = self._load(existing, snapshot.run_id)
                if recorded.snapshot_checksum != snapshot.snapshot_checksum:
                    raise _error("reference grant binding already contains different content", "REF_SNAPSHOT_CONFLICT")
                return recorded.snapshot_ref
            self._write_artifact(artifact, content)
            request = EventPublishRequest(
                event_id=self._event_id(snapshot.run_id, snapshot.binding_key),
                event_type=REF_SNAPSHOT_EVENT_TYPE,
                data_schema=REF_SNAPSHOT_EVENT_SCHEMA,
                source=REF_SNAPSHOT_EVENT_SOURCE,
                occurred_at=self._clock(),
                stream_id=f"run:{snapshot.run_id}",
                subject=snapshot.stage_id,
                correlation_id=snapshot.run_id,
                tenant_id=self._tenant_id,
                business_context=self._business_context(snapshot),
                producer=ProducerIdentity(component=REF_SNAPSHOT_EVENT_SOURCE, version="1"),
                payload=self._event_payload(snapshot, artifact),
            )
            try:
                committed = self._runtime.publish(request, expected_last_sequence=high)
            except (EventStreamVersionConflictError, EventIdentityCollisionError):
                continue
            if self._load(committed, snapshot.run_id) != snapshot:
                raise _error("reference snapshot commit returned different evidence")
            return snapshot.snapshot_ref
        raise _error("reference grant exceeded bounded append retries", "REF_SNAPSHOT_CONTENTION")

    def get(self, *, run_id: str, snapshot_ref: str) -> RefAuthoritySnapshot:
        run_id = identifier(run_id, "run_id")
        snapshot_ref = checksum(snapshot_ref, "snapshot_ref")
        events, _ = self._read_events(run_id)
        event = self._find_event(events, "snapshot_ref", snapshot_ref)
        if event is None:
            raise _error("reference snapshot is not durably committed", "REF_SNAPSHOT_MISSING")
        # A dependent child has one input-admission parent plus explicit edges
        # to prior producer results. Verify the whole bounded DAG once, using
        # this fixed event prefix rather than recursively rereading the stream.
        by_ref = {item.payload["snapshot_ref"]: item for item in events}
        pending = [event]
        loaded: dict[str, RefAuthoritySnapshot] = {}
        while pending:
            current = pending.pop()
            ref = current.payload["snapshot_ref"]
            if ref in loaded:
                continue
            if len(loaded) >= _MAX_AUTHORITY_GRAPH_SNAPSHOTS:
                raise _error("reference authority graph exceeds its bounded size", "REF_SNAPSHOT_SIZE_EXCEEDED")
            child = self._load(current, run_id)
            loaded[ref] = child
            sources = {item.source_snapshot_ref for item in child.dependency_bindings}
            if child.parent_snapshot_ref is not None:
                sources.add(child.parent_snapshot_ref)
            for source_ref in sources:
                source_event = by_ref.get(source_ref)
                if source_event is None or source_event.stream_sequence >= current.stream_sequence:
                    raise _error("reference grant has no prior committed source", "REF_SNAPSHOT_PARENT_MISSING")
                pending.append(source_event)
        for child in loaded.values():
            if child.parent_snapshot_ref is not None:
                child.validate_parent(loaded[child.parent_snapshot_ref])
            child.validate_dependency_sources(loaded)
        return loaded[snapshot_ref]

    def find(self, *, run_id: str, binding_key: str) -> RefAuthoritySnapshot | None:
        run_id = identifier(run_id, "run_id")
        binding_key = checksum(binding_key, "binding_key")
        events, _ = self._read_events(run_id)
        event = self._find_event(events, "binding_key", binding_key)
        return None if event is None else self.get(run_id=run_id, snapshot_ref=event.payload["snapshot_ref"])

    def _read_events(self, run_id: str) -> tuple[tuple[StoredEvent, ...], int]:
        stream = f"run:{identifier(run_id, 'run_id')}"
        high = self._reader.get_stream_high_watermark(stream, tenant_id=self._tenant_id)
        if high is None:
            return (), 0
        cursor = None
        events: list[StoredEvent] = []
        bindings: set[str] = set()
        refs: set[str] = set()
        while True:
            page = self._reader.read_stream(StreamReadRequest(
                stream_id=stream, tenant_id=self._tenant_id, cursor=cursor,
                through_sequence=high, limit=500,
                event_types=frozenset({REF_SNAPSHOT_EVENT_TYPE}),
            ))
            for event in page.events:
                payload = self._validate_event(event, run_id)
                if payload["binding_key"] in bindings or payload["snapshot_ref"] in refs:
                    raise _error("reference snapshot history repeats a committed identity", "REF_SNAPSHOT_CONFLICT")
                bindings.add(payload["binding_key"])
                refs.add(payload["snapshot_ref"])
                events.append(event)
            if page.next_cursor is None:
                break
            if page.next_cursor == cursor:
                raise _error("reference snapshot history cursor did not advance")
            cursor = page.next_cursor
        return tuple(events), high

    def _validate_event(self, event: StoredEvent, run_id: str) -> Mapping[str, Any]:
        event.verify_integrity()
        if (
            event.event_type != REF_SNAPSHOT_EVENT_TYPE
            or event.data_schema != REF_SNAPSHOT_EVENT_SCHEMA
            or event.source != REF_SNAPSHOT_EVENT_SOURCE
            or event.stream_id != f"run:{run_id}"
            or event.tenant_id != self._tenant_id
            or event.business_context.run_id != run_id
        ):
            raise _error("reference snapshot event is outside its canonical stream")
        payload = thaw_canonical_json(event.payload or {})
        fields = {"snapshot_ref", "binding_key", "stage_id", "phase", "artifact_checksum", "artifact_size_bytes"}
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise _error("reference snapshot event payload is invalid")
        for name in ("snapshot_ref", "binding_key", "artifact_checksum"):
            checksum(payload[name], name)
        identifier(payload["stage_id"], "stage_id")
        size = positive_int(payload["artifact_size_bytes"], "artifact_size_bytes")
        if size > _MAX_SNAPSHOT_BYTES or payload["phase"] not in set(RefSnapshotPhase):
            raise _error("reference snapshot event exceeds its schema bounds")
        if (
            event.event_id != self._event_id(run_id, payload["binding_key"])
            or event.subject != payload["stage_id"]
            or event.correlation_id != run_id
            or event.producer != ProducerIdentity(component=REF_SNAPSHOT_EVENT_SOURCE, version="1")
        ):
            raise _error("reference snapshot event envelope differs from its canonical binding")
        return payload

    def _event_id(self, run_id: str, binding_key: str) -> str:
        return "refgrant_" + checksum_for({
            "tenant": self._tenant_id, "run": run_id, "binding": binding_key,
        }).removeprefix("sha256:")

    @staticmethod
    def _find_event(events: tuple[StoredEvent, ...], field: str, value: str) -> StoredEvent | None:
        return next((event for event in events if event.payload[field] == value), None)

    def _load(self, event: StoredEvent, run_id: str) -> RefAuthoritySnapshot:
        payload = self._validate_event(event, run_id)
        artifact = self._artifact_ref(run_id, payload["snapshot_ref"], payload["artifact_checksum"], payload["artifact_size_bytes"])
        try:
            content = self._artifacts.read(artifact)
            if len(content) != artifact.size_bytes or checksum_for_bytes(content) != payload["artifact_checksum"]:
                raise _error("reference snapshot artifact failed checksum verification")
            snapshot = RefAuthoritySnapshot.from_dict(json.loads(content.decode("utf-8")))
            if canonical_json_bytes(snapshot.to_dict()) != content:
                raise _error("reference snapshot artifact is not canonical")
        except (OSError, ValueError, TypeError) as exc:
            raise _error("committed reference snapshot artifact is missing or corrupt") from exc
        if (
            snapshot.run_id != run_id
            or self._event_payload(snapshot, artifact) != payload
            or self._business_context(snapshot) != event.business_context
        ):
            raise _error("reference snapshot artifact differs from its committed identity")
        return snapshot

    def _write_artifact(self, artifact: ArtifactRef, content: bytes) -> None:
        try:
            if self._artifacts.exists(artifact):
                if self._artifacts.read(artifact) != content:
                    raise _error("reference snapshot artifact identity conflicts")
                return
            written = self._artifacts.write(ArtifactWriteRequest(
                artifact_id=artifact.artifact_id, run_id=artifact.run_id,
                artifact_type=artifact.artifact_type, relative_path=artifact.path,
                content=content, content_type=artifact.content_type, redacted=True,
            ))
            fields = ("artifact_id", "run_id", "artifact_type", "scope_kind", "path", "content_type", "size_bytes", "checksum", "redacted")
            if any(getattr(written, name) != getattr(artifact, name) for name in fields):
                raise _error("reference snapshot artifact store returned conflicting metadata")
        except (OSError, ValueError, TypeError) as exc:
            raise _error("reference snapshot artifact could not be persisted", "REF_SNAPSHOT_WRITE_FAILED") from exc

    @staticmethod
    def _artifact_ref(run_id: str, snapshot_ref: str, content_checksum: str, size: int) -> ArtifactRef:
        digest = checksum(snapshot_ref, "snapshot_ref").removeprefix("sha256:")
        return ArtifactRef(
            artifact_id="ref_authority_" + digest, run_id=run_id,
            artifact_type=_ARTIFACT_TYPE, path=f"harness/ref-authority/{digest}.json",
            content_type="application/json", size_bytes=size,
            checksum=checksum(content_checksum, "artifact_checksum").removeprefix("sha256:"), redacted=True,
        )

    @staticmethod
    def _event_payload(snapshot: RefAuthoritySnapshot, artifact: ArtifactRef) -> dict[str, Any]:
        return {
            "snapshot_ref": snapshot.snapshot_ref,
            "binding_key": snapshot.binding_key,
            "stage_id": snapshot.stage_id,
            "phase": snapshot.phase.value,
            "artifact_checksum": "sha256:" + artifact.checksum,
            "artifact_size_bytes": artifact.size_bytes,
        }

    @staticmethod
    def _business_context(snapshot: RefAuthoritySnapshot) -> BusinessContext:
        execution = snapshot.execution_identity
        return BusinessContext(
            run_id=execution.run_id, graph_id=execution.graph_id,
            graph_version=execution.graph_version, graph_ref=execution.graph_ref,
            graph_checksum=execution.graph_checksum, stage_id=snapshot.stage_id,
            node_instance_id=execution.node_instance_id,
            task_id=None if snapshot.attempt_identity is None else snapshot.attempt_identity.task_id,
        )


def checksum_for_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()
