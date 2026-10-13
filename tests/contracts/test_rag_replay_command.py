from __future__ import annotations

import json

import pytest

import scripts.dev as dev
from framework.harness.rag.models import RAGSessionStatus, RAGTranscript
from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore


def test_replay_rag_parser_accepts_transcript_id() -> None:
    args = dev.build_parser().parse_args([
        "replay-rag",
        "transcript-1",
        "--transcript-root",
        "tmp/transcripts",
        "--memory-namespace",
        "research.public",
    ])

    assert args.command == "replay-rag"
    assert args.transcript == "transcript-1"
    assert args.transcript_root == "tmp/transcripts"
    assert args.memory_namespace == "research.public"


def test_replay_rag_command_requires_explicit_actor_scope(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    store.persist(
        _transcript("transcript-scoped", "rag_answer_returned", RAGSessionStatus.ANSWERED),
        actor_scope={"memory_namespace": "research.public"},
    )

    with pytest.raises(ValueError, match="explicit actor scope"):
        dev.main([
            "replay-rag",
            "transcript-scoped",
            "--transcript-root",
            str(tmp_path),
        ])


def test_replay_rag_command_prints_replay_report(tmp_path, capsys) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    store.persist(
        _transcript("transcript-ok", "rag_answer_returned", RAGSessionStatus.ANSWERED),
        actor_scope={"memory_namespace": "research.public"},
    )

    exit_code = dev.main([
        "replay-rag", "transcript-ok", "--transcript-root", str(tmp_path),
        "--memory-namespace", "research.public",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transcript_id"] == "transcript-ok"
    assert payload["status"] == "answered"
    assert payload["replayable"] is True


def test_replay_rag_command_returns_nonzero_for_non_replayable_transcript(tmp_path, capsys) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    store.persist(
        _transcript("transcript-bad", "rag_plan_verified", RAGSessionStatus.ANSWERED),
        actor_scope={"memory_namespace": "research.public"},
    )

    exit_code = dev.main([
        "replay-rag", "transcript-bad", "--transcript-root", str(tmp_path),
        "--memory-namespace", "research.public",
    ])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["replayable"] is False
    assert payload["errors"]


def _transcript(transcript_id: str, event_type: str, status: RAGSessionStatus) -> RAGTranscript:
    return RAGTranscript(
        transcript_id=transcript_id,
        session_id="session-1",
        events=(
            {
                "event_type": event_type,
                "payload": {"decision_type": "return_answer"},
            },
        ),
        status=status,
    )
