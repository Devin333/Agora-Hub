from __future__ import annotations

from types import SimpleNamespace

import pytest

from framework.harness.rag.models import RAGSessionStatus, RAGTranscript
from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore, SCHEMA_VERSION


_SCOPE = {"user_id": "user-1", "memory_namespace": "research:user:user-1"}


def test_transcript_store_persists_and_loads_actor_scoped_transcript(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    transcript = _transcript()
    _record_transcript_events(store, transcript)

    artifact = store.persist(transcript, actor_scope=_SCOPE)

    assert artifact.path.exists()
    assert artifact.transcript_id == "transcript-1"
    assert artifact.to_dict()["schema_version"] == SCHEMA_VERSION
    assert store.path_for("transcript-1") == artifact.path
    assert store.load("transcript-1", actor_scope=_SCOPE)["transcript"]["transcript_id"] == "transcript-1"
    assert store.transcript_payload("transcript-1", actor_scope=_SCOPE)["status"] == "answered"
    assert store.load_journal("session-1", actor_scope=_SCOPE)["finalized"] is True


def test_transcript_store_keeps_interrupted_phase_events_readable(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    spec = SimpleNamespace(session_id="session-interrupted")
    store.record_event(
        spec=spec,
        sequence=0,
        event_type="rag_session_started",
        payload={"phase": "plan"},
        actor_scope=_SCOPE,
    )
    store.record_event(
        spec=spec,
        sequence=1,
        event_type="rag_plan_verified",
        payload={"passed": True},
        actor_scope=_SCOPE,
    )

    journal = store.load_journal("session-interrupted", actor_scope=_SCOPE)
    assert journal is not None
    assert journal["finalized"] is False
    assert [event["event_type"] for event in journal["events"]] == [
        "rag_session_started",
        "rag_plan_verified",
    ]
    with pytest.raises(PermissionError):
        store.load_journal("session-interrupted", actor_scope={"user_id": "other"})


def test_transcript_store_rejects_transcript_that_disagrees_with_journal(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    spec = SimpleNamespace(session_id="session-1")
    store.record_event(
        spec=spec,
        sequence=0,
        event_type="rag_session_started",
        payload={},
        actor_scope=_SCOPE,
    )

    with pytest.raises(ValueError, match="durable event journal"):
        store.persist(_transcript(), actor_scope=_SCOPE)


def test_transcript_store_loads_uri_transcript_ids_by_hash(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    transcript_id = "rag-transcript://paper-rag-ask-session-abc/12345678"
    transcript = _transcript(transcript_id=transcript_id)
    _record_transcript_events(store, transcript)
    store.persist(transcript, actor_scope=_SCOPE)

    assert store.transcript_payload(transcript_id, actor_scope=_SCOPE)["transcript_id"] == transcript_id


def _record_transcript_events(store: PaperRagTranscriptFileStore, transcript: RAGTranscript) -> None:
    spec = SimpleNamespace(session_id=transcript.session_id)
    for sequence, event in enumerate(transcript.events):
        store.record_event(
            spec=spec,
            sequence=sequence,
            event_type=str(event["event_type"]),
            payload=dict(event["payload"]),
            actor_scope=_SCOPE,
        )


def _transcript(transcript_id: str = "transcript-1") -> RAGTranscript:
    return RAGTranscript(
        transcript_id=transcript_id,
        session_id="session-1",
        events=(
            {
                "event_type": "rag_answer_returned",
                "payload": {"decision_type": "return_answer"},
            },
        ),
        status=RAGSessionStatus.ANSWERED,
    )
