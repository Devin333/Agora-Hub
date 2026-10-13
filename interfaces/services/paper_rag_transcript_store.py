from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from framework.harness.rag.models import RAGTranscript
from framework.shared.json import to_jsonable
from framework.shared.time import format_datetime, utc_now
from interfaces.services.json_file_store import (
    JsonFileInvalidError,
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)

DEFAULT_TRANSCRIPT_ROOT = Path(".newsroom") / "research" / "rag" / "transcripts"
SCHEMA_VERSION = "newsroom.paper_rag_transcript.v2"


@dataclass(frozen=True)
class PaperRagTranscriptArtifact:
    transcript_id: str
    path: Path
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transcript_id": self.transcript_id,
            "persisted": True,
        }


class PaperRagTranscriptFileStore:
    """Atomic, checksummed, actor-scoped audit store for gated Paper RAG."""

    def __init__(self, root: str | Path = DEFAULT_TRANSCRIPT_ROOT) -> None:
        self.root = Path(root).expanduser().resolve()

    def record_event(
        self,
        *,
        spec: Any,
        sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
        actor_scope: Mapping[str, Any],
    ) -> None:
        session_id = str(getattr(spec, "session_id", "") or "").strip()
        if not session_id:
            raise ValueError("RAG event journal requires session_id")
        scope = _actor_scope(actor_scope)
        path = self.journal_path_for(session_id)
        event = {
            "sequence": int(sequence),
            "event_type": str(event_type or "").strip(),
            "payload": to_jsonable(dict(payload)),
        }
        if event["sequence"] < 0 or not event["event_type"]:
            raise ValueError("RAG event journal entry is invalid")
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_file(path) as resolved:
            journal = read_json_object_unlocked(resolved, default={})
            if journal:
                _validate_journal(journal, expected_scope=scope)
                if journal.get("session_id") != session_id:
                    raise ValueError("RAG event journal session identity mismatch")
            else:
                journal = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "event_journal",
                    "session_id": session_id,
                    "actor_scope": scope,
                    "events": [],
                    "finalized": False,
                }
            events = journal.setdefault("events", [])
            if len(events) > event["sequence"]:
                if events[event["sequence"]] != event:
                    raise ValueError("RAG event sequence is already bound to different content")
                return
            if len(events) != event["sequence"]:
                raise ValueError("RAG event journal sequence is not contiguous")
            events.append(event)
            journal["updated_at"] = format_datetime(utc_now())
            journal["journal_checksum"] = _checksum({"session_id": session_id, "events": events})
            write_json_object_unlocked(resolved, journal)

    def load_journal(
        self,
        session_id: str,
        *,
        actor_scope: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        path = self.journal_path_for(session_id)
        with locked_json_file(path) as resolved:
            if not resolved.exists():
                return None
            journal = read_json_object_unlocked(resolved, default={}, strict=True)
        _validate_journal(journal, expected_scope=_actor_scope(actor_scope))
        return journal

    def persist(
        self,
        transcript: RAGTranscript | Mapping[str, Any],
        *,
        actor_scope: Mapping[str, Any],
    ) -> PaperRagTranscriptArtifact:
        payload = _coerce_transcript_payload(transcript)
        transcript_id = _transcript_id(payload)
        scope = _actor_scope(actor_scope)
        session_id = str(payload.get("session_id") or "").strip()
        journal = self.load_journal(session_id, actor_scope=scope)
        if journal is not None:
            journal_events = [
                {"event_type": item["event_type"], "payload": item["payload"]}
                for item in journal.get("events", [])
            ]
            if journal_events != list(payload.get("events") or ()):
                raise ValueError("RAG transcript does not match its durable event journal")
        path = self.path_for(transcript_id)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "transcript_id": transcript_id,
            "session_id": session_id,
            "stored_at": format_datetime(utc_now()),
            "actor_scope": scope,
            "transcript_checksum": _checksum(payload),
            "transcript": payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_file(path) as resolved:
            existing = read_json_object_unlocked(resolved, default={})
            if existing:
                _validate_envelope(existing, expected_scope=scope)
                if existing.get("transcript_checksum") != envelope["transcript_checksum"]:
                    raise ValueError("RAG transcript identity is already bound to different content")
            else:
                write_json_object_unlocked(resolved, envelope)
        if journal is not None:
            journal_path = self.journal_path_for(session_id)
            with locked_json_file(journal_path) as resolved:
                current = read_json_object_unlocked(resolved, default={}, strict=True)
                _validate_journal(current, expected_scope=scope)
                current["finalized"] = True
                current["transcript_id"] = transcript_id
                current["updated_at"] = format_datetime(utc_now())
                current["journal_checksum"] = _checksum({"session_id": session_id, "events": current["events"]})
                write_json_object_unlocked(resolved, current)
        return PaperRagTranscriptArtifact(transcript_id=transcript_id, path=path)

    def load(
        self,
        transcript_id: str,
        *,
        actor_scope: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = self.path_for(transcript_id)
        with locked_json_file(path) as resolved:
            if not resolved.exists():
                raise FileNotFoundError(f"RAG transcript artifact not found: {transcript_id}")
            payload = read_json_object_unlocked(resolved, default={}, strict=True)
        _validate_envelope(payload, expected_scope=_actor_scope(actor_scope))
        return payload

    def transcript_payload(
        self,
        transcript_id: str,
        *,
        actor_scope: Mapping[str, Any],
    ) -> dict[str, Any]:
        return dict(self.load(transcript_id, actor_scope=actor_scope)["transcript"])

    def find_by_session_id(
        self,
        session_id: str,
        *,
        actor_scope: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        expected = str(session_id or "").strip()
        scope = _actor_scope(actor_scope)
        if not expected or not self.root.exists():
            return None
        candidates: list[tuple[str, dict[str, Any]]] = []
        for path in self.root.glob("*.json"):
            try:
                with locked_json_file(path) as resolved:
                    envelope = read_json_object_unlocked(resolved, default={}, strict=True)
                _validate_envelope(envelope, expected_scope=scope)
            except (JsonFileInvalidError, OSError, ValueError, PermissionError):
                continue
            if envelope.get("session_id") == expected:
                candidates.append((str(envelope.get("stored_at") or ""), envelope))
        return dict(max(candidates, key=lambda item: item[0])[1]) if candidates else None

    def journal_path_for(self, session_id: str) -> Path:
        normalized = _normalize_transcript_id(session_id)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.root / f"journal-{digest}.json"

    def path_for(self, transcript_id: str) -> Path:
        normalized = _normalize_transcript_id(transcript_id)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def resolve(self, transcript_id: str) -> Path:
        path = self.path_for(transcript_id)
        if not path.exists():
            raise FileNotFoundError(f"RAG transcript artifact not found: {transcript_id}")
        return path



def _validate_journal(value: Any, *, expected_scope: Mapping[str, str]) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported RAG event journal schema")
    if value.get("kind") != "event_journal" or not str(value.get("session_id") or "").strip():
        raise ValueError("RAG event journal identity is invalid")
    events = value.get("events")
    if not isinstance(events, list):
        raise ValueError("RAG event journal events are invalid")
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("sequence") != index:
            raise ValueError("RAG event journal sequence is invalid")
        if not str(event.get("event_type") or "").strip() or not isinstance(event.get("payload"), dict):
            raise ValueError("RAG event journal entry is invalid")
    expected_checksum = _checksum({"session_id": value["session_id"], "events": events})
    if value.get("journal_checksum") != expected_checksum:
        raise ValueError("RAG event journal checksum mismatch")
    if _actor_scope(value.get("actor_scope")) != dict(expected_scope):
        raise PermissionError("RAG event journal is not visible to this actor scope")

def _validate_envelope(value: Any, *, expected_scope: Mapping[str, str]) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported RAG transcript artifact schema")
    transcript = value.get("transcript")
    if not isinstance(transcript, dict) or value.get("transcript_id") != transcript.get("transcript_id"):
        raise ValueError("RAG transcript artifact requires a bound transcript")
    if value.get("session_id") != transcript.get("session_id"):
        raise ValueError("RAG transcript session identity mismatch")
    if value.get("transcript_checksum") != _checksum(transcript):
        raise ValueError("RAG transcript checksum mismatch")
    if _actor_scope(value.get("actor_scope")) != dict(expected_scope):
        raise PermissionError("RAG transcript is not visible to this actor scope")


def _coerce_transcript_payload(transcript: RAGTranscript | Mapping[str, Any]) -> dict[str, Any]:
    return transcript.to_dict() if isinstance(transcript, RAGTranscript) else dict(transcript)


def _actor_scope(value: Mapping[str, Any] | Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("RAG transcript actor scope is required")
    result = {
        key: str(value[key]).strip()
        for key in ("tenant_id", "user_id", "memory_namespace")
        if value.get(key) is not None and str(value[key]).strip()
    }
    if not result:
        raise ValueError("RAG transcript actor scope is required")
    return result


def _checksum(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(to_jsonable(dict(payload)), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _transcript_id(payload: Mapping[str, Any]) -> str:
    return _normalize_transcript_id(str(payload.get("transcript_id") or ""))


def _normalize_transcript_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 1000 or any(ord(char) < 0x20 for char in normalized):
        raise ValueError("transcript_id is invalid")
    return normalized


__all__ = [
    "DEFAULT_TRANSCRIPT_ROOT",
    "SCHEMA_VERSION",
    "PaperRagTranscriptArtifact",
    "PaperRagTranscriptFileStore",
]
