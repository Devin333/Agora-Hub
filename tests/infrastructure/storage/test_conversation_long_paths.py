from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from framework.shared.windows_paths import storage_io_path
from infrastructure.storage.conversation import (
    AgentIterationCheckpoint,
    AgentMessageRecord,
    ConversationCursor,
    LocalJsonConversationStore,
)
from infrastructure.storage.security import REDACTED_VALUE

UTC = timezone.utc
_CONVERSATION_ID = "child-agent-" + "6ab6d1e74ccda28e3f0e4257a5edd3684e199d8ae8ac2f8ea5349ef95ea84b52"
_OWNED_PREFIX = "newsroom-owned-conversation-long-path-"


@pytest.fixture
def windows_long_conversation_root() -> Path:
    if os.name != "nt":
        pytest.skip("Windows native namespace behavior is Windows-specific")

    owner = Path(tempfile.mkdtemp(prefix=_OWNED_PREFIX)).resolve()
    root = (
        owner
        / ".newsroom"
        / "runs"
        / "_state"
        / "research-dynamic-conversations"
        / "research_analysis_contribution"
    )
    while len(os.fspath(root / _CONVERSATION_ID / "iteration_checkpoint.json")) <= 280:
        root /= "owned-long-parent-segment"

    try:
        assert len(os.fspath(root / _CONVERSATION_ID / "messages.jsonl")) > 260
        yield root
    finally:
        temp_root = Path(tempfile.gettempdir()).resolve()
        assert owner.name.startswith(_OWNED_PREFIX)
        assert os.path.commonpath((os.fspath(owner), os.fspath(temp_root))) == os.fspath(
            temp_root
        )
        shutil.rmtree(storage_io_path(owner), ignore_errors=False)


def test_local_json_conversation_store_supports_complete_long_path_lifecycle(
    windows_long_conversation_root: Path,
) -> None:
    root = windows_long_conversation_root
    store = LocalJsonConversationStore(root)
    secret = "sk" + "-conversationlongpathsecret123456"

    assert store.root == root
    assert not _has_windows_namespace(store.root)
    assert store.read_messages(_CONVERSATION_ID) == []
    assert store.get_summary(_CONVERSATION_ID) is None
    assert store.get_compaction(_CONVERSATION_ID) is None
    assert store.read_cursor(_CONVERSATION_ID) is None
    assert store.read_iteration_checkpoint(_CONVERSATION_ID) is None

    messages = [
        _graph_message("message-1", "user", f"Inspect {secret}"),
        _graph_message("message-2", "assistant", "Analyze the evidence"),
        _graph_message("message-3", "judge", "Accept the grounded result"),
    ]
    message_path = store.append_message(_CONVERSATION_ID, messages[0])
    store.append_message(_CONVERSATION_ID, messages[1])
    store.append_message(_CONVERSATION_ID, messages[2])

    restored_messages = store.read_messages(_CONVERSATION_ID)
    assert [message.message_id for message in restored_messages] == [
        "message-1",
        "message-2",
        "message-3",
    ]
    assert restored_messages[0].content == f"Inspect {REDACTED_VALUE}"
    assert store.read_messages(_CONVERSATION_ID, limit=1) == [restored_messages[-1]]
    _assert_graph_identity(restored_messages)
    _assert_public_path(
        message_path,
        root / _CONVERSATION_ID / "messages.jsonl",
    )

    summary_path = store.write_summary(
        _CONVERSATION_ID,
        f"Summary contains {secret}",
    )
    assert store.get_summary(_CONVERSATION_ID) == f"Summary contains {REDACTED_VALUE}"
    _assert_public_path(summary_path, root / _CONVERSATION_ID / "summary.json")

    cursor = ConversationCursor(
        conversation_id=_CONVERSATION_ID,
        message_offset=3,
        message_id="message-3",
        run_id="run-1",
        node_instance_id="analyze:1",
        graph_checkpoint_ref="checkpoint://run-1/3",
        updated_at=datetime(2026, 10, 15, 4, 0, tzinfo=UTC),
        metadata={"api_key": secret, "phase": "verify"},
    )
    cursor_path = store.write_cursor(cursor)
    restored_cursor = store.read_cursor(_CONVERSATION_ID)
    assert restored_cursor is not None
    assert restored_cursor.run_id == "run-1"
    assert restored_cursor.node_instance_id == "analyze:1"
    assert restored_cursor.graph_checkpoint_ref == "checkpoint://run-1/3"
    assert restored_cursor.metadata["api_key"] == REDACTED_VALUE
    assert restored_cursor.metadata["phase"] == "verify"
    _assert_public_path(cursor_path, root / _CONVERSATION_ID / "cursor.json")

    checkpoint = AgentIterationCheckpoint(
        conversation_id=_CONVERSATION_ID,
        agent_id="analyst",
        iteration=2,
        status="accepted",
        stop_reason="final_output_accepted",
        run_id="run-1",
        node_instance_id="analyze:1",
        graph_checkpoint_ref="checkpoint://run-1/3",
        message_id="message-3",
        trace_summary={"iteration_count": 2},
        diagnostics_summary={"summary": f"Accepted without exposing {secret}"},
        llm_call_artifact_ids=["analyst:llm_call:2"],
        updated_at=datetime(2026, 10, 15, 4, 1, tzinfo=UTC),
        metadata={"phase": "verify"},
    )
    checkpoint_path = store.write_iteration_checkpoint(checkpoint)
    restored_checkpoint = store.read_iteration_checkpoint(_CONVERSATION_ID)
    assert restored_checkpoint is not None
    assert restored_checkpoint.run_id == "run-1"
    assert restored_checkpoint.node_instance_id == "analyze:1"
    assert restored_checkpoint.graph_checkpoint_ref == "checkpoint://run-1/3"
    assert restored_checkpoint.diagnostics_summary["summary"] == (
        f"Accepted without exposing {REDACTED_VALUE}"
    )
    _assert_public_path(
        checkpoint_path,
        root / _CONVERSATION_ID / "iteration_checkpoint.json",
    )

    compaction = store.compact_messages(_CONVERSATION_ID, keep_last=2)
    assert compaction is not None
    assert store.get_compaction(_CONVERSATION_ID) == compaction
    assert compaction.original_message_count == 3
    assert compaction.compacted_message_count == 1
    assert compaction.retained_message_count == 2
    compacted_messages = store.read_messages(_CONVERSATION_ID)
    assert [message.message_id for message in compacted_messages] == [
        "compaction-message-1",
        "message-2",
        "message-3",
    ]
    assert compacted_messages[0].content["summary"] == compaction.summary
    _assert_graph_identity(compacted_messages)

    artifact_names = {
        "messages.jsonl",
        "summary.json",
        "compaction.json",
        "cursor.json",
        "iteration_checkpoint.json",
    }
    conversation_io_root = storage_io_path(root / _CONVERSATION_ID)
    assert {path.name for path in conversation_io_root.iterdir()} == artifact_names
    for artifact_name in artifact_names:
        persisted = (conversation_io_root / artifact_name).read_text(encoding="utf-8")
        assert secret not in persisted


def _graph_message(message_id: str, role: str, content: str) -> AgentMessageRecord:
    return AgentMessageRecord(
        message_id=message_id,
        conversation_id=_CONVERSATION_ID,
        role=role,
        content=content,
        created_at=datetime(2026, 10, 15, 3, int(message_id[-1]), tzinfo=UTC),
        scope_kind="graph",
        run_id="run-1",
        graph_id="research.dynamic",
        graph_version="1",
        graph_ref="research.dynamic@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze:1",
        graph_checkpoint_ref="checkpoint://run-1/3",
        activity_id="activity-1",
        attempt=1,
    )


def _assert_graph_identity(messages: list[AgentMessageRecord]) -> None:
    for message in messages:
        assert message.scope_kind == "graph"
        assert message.run_id == "run-1"
        assert message.graph_id == "research.dynamic"
        assert message.graph_version == "1"
        assert message.graph_ref == "research.dynamic@1"
        assert message.graph_checksum == "sha256:" + "a" * 64
        assert message.node_id == "analyze"
        assert message.node_instance_id == "analyze:1"
        assert message.graph_checkpoint_ref == "checkpoint://run-1/3"
        assert message.activity_id == "activity-1"
        assert message.attempt == 1


def _assert_public_path(actual: Path, expected: Path) -> None:
    assert actual == expected
    assert not _has_windows_namespace(actual)
    assert storage_io_path(actual).is_file()


def _has_windows_namespace(path: Path) -> bool:
    return os.fspath(path).casefold().startswith("\\\\?\\")
