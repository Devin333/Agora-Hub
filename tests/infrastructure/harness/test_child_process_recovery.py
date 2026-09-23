from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from framework.events import (
    EventRuntime,
    EventSchemaCatalog,
    TransactionalStateSnapshot,
    thaw_canonical_json,
)
from framework.harness.subagents import CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE
from framework.shared.json import stable_json_dumps
from infrastructure.storage.events.sqlite import SQLiteEventStore


PYTHON = sys.executable
DRIVER = Path(__file__).with_name("child_process_recovery_driver.py")
SCOPE = "research.dynamic-analysis.children"


def _start(
    tmp_path: Path, mode: str, *, owner: str, options: dict[str, object] | None = None
):
    database = tmp_path / "_records" / "events.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    from infrastructure.storage.events.sqlite import SQLiteEventStore

    SQLiteEventStore(database)
    signal = tmp_path / f"{mode}-{owner}.json"
    command = [
        PYTHON,
        str(DRIVER),
        mode,
        str(database),
        str(signal),
        owner,
        json.dumps(options or {}),
    ]
    return (
        subprocess.Popen(command, cwd=Path(__file__).resolve().parents[3]),
        database,
        signal,
    )


def _await(path: Path, timeout: float = 15.0) -> dict[str, object]:
    import time

    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"process barrier did not arrive: {path}")
        time.sleep(0.01)
    return json.loads(path.read_text(encoding="utf-8"))


def _finish(process: subprocess.Popen, *, terminate: bool = False) -> None:
    if terminate and process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)


def _counter(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT count FROM effects WHERE id = 1").fetchone()
    return 0 if row is None else int(row[0])


def test_terminal_barrier_survives_process_kill_and_restart_without_worker_reinvoke(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release-terminal"
    counter_db = tmp_path / "terminal-invocations.sqlite3"
    process, database, signal = _start(
        tmp_path,
        "terminal",
        owner="owner-terminal",
        options={"release_path": str(release), "counter_db": str(counter_db)},
    )
    try:
        committed = _await(signal)
        process.terminate()
        process.wait(timeout=15)

        outcome = tmp_path / "recovered-terminal.json"
        recover, _database, recovered_signal = _start(
            tmp_path,
            "recover",
            owner="owner-recovered-terminal",
            options={
                "outcome_path": str(outcome),
                "clock_offset": 60,
                "counter_db": str(counter_db),
            },
        )
        try:
            assert _await(recovered_signal)["recovered"] == "SUCCEEDED"
            assert recover.wait(timeout=15) == 0
            payload = json.loads(outcome.read_text(encoding="utf-8"))
        finally:
            _finish(recover)
        assert payload["state"] == "SUCCEEDED"
        assert payload["receipt"]["receipt_checksum"] == committed["receipt_checksum"]
        assert payload["result"] == {"candidate": "durably-committed"}
        assert _counter(counter_db) == 1
    finally:
        _finish(process)


def test_external_effect_before_terminal_commit_blocks_replay_and_preserves_capacity(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release-effect"
    effect_barrier = tmp_path / "effect-committed"
    counter_db = tmp_path / "business-counter.sqlite3"
    process, database, signal = _start(
        tmp_path,
        "effect",
        owner="owner-effect",
        options={
            "release_path": str(release),
            "effect_barrier": str(effect_barrier),
            "counter_db": str(counter_db),
        },
    )
    try:
        _await(signal)
        assert _await(effect_barrier)["effect_committed"] is True
        assert _counter(counter_db) == 1
        process.terminate()
        process.wait(timeout=15)

        outcome = tmp_path / "recovered-effect.json"
        recover, _database, recovered_signal = _start(
            tmp_path,
            "recover",
            owner="owner-recovered-effect",
            options={
                "outcome_path": str(outcome),
                "clock_offset": 60,
                "counter_db": str(counter_db),
            },
        )
        try:
            assert _await(recovered_signal)["recovered"] in {"LOST", "INDETERMINATE"}
            assert recover.wait(timeout=15) == 0
            payload = json.loads(outcome.read_text(encoding="utf-8"))
        finally:
            _finish(recover)
        assert payload["state"] in {"LOST", "INDETERMINATE"}
        assert _counter(counter_db) == 1
        blocked, _database, blocked_signal = _start(
            tmp_path,
            "takeover",
            owner="owner-replacement",
            options={"clock_offset": 61},
        )
        try:
            assert _await(blocked_signal)["code"] == "child_capacity_exhausted"
        finally:
            _finish(blocked)
    finally:
        _finish(process)


def test_live_owner_competition_and_expired_takeover_fence_old_owner(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release-owner"
    submit = tmp_path / "submit-owner"
    submit_outcome = tmp_path / "submit-owner-result.json"
    first, database, first_signal = _start(
        tmp_path,
        "hold",
        owner="owner-first",
        options={
            "release_path": str(release),
            "lease_seconds": 10.0,
            "spawn_child": True,
            "submit_path": str(submit),
            "submit_outcome": str(submit_outcome),
        },
    )
    second = None
    takeover = None
    try:
        assert _await(first_signal)["owner"] == "owner-first"
        second, _database, second_signal = _start(
            tmp_path, "compete", owner="owner-second", options={"clock_offset": 0}
        )
        assert _await(second_signal)["code"] == "child_owner_conflict"
        _finish(second)

        takeover, _database, takeover_signal = _start(
            tmp_path,
            "takeover",
            owner="owner-takeover",
            options={"clock_offset": 60, "lease_seconds": 30.0},
        )
        # Existing active occupancy is still counted; takeover must not turn
        # an expired control lease into permission to duplicate the child.
        assert _await(takeover_signal)["code"] == "child_capacity_exhausted"
        submit.write_text("submit", encoding="utf-8")
        old_owner_result = _await(submit_outcome)
        assert old_owner_result["code"] == "child_owner_lost"
    finally:
        if second is not None:
            _finish(second)
        if takeover is not None:
            _finish(takeover)
        release.write_text("release", encoding="utf-8")
        _finish(first)


@pytest.mark.parametrize("corruption", ("snapshot_checksum", "malformed_spawn"))
def test_corrupt_transactional_spawn_log_fails_closed_before_new_admission(
    tmp_path: Path,
    corruption: str,
) -> None:
    release = tmp_path / "release-corrupt"
    process, database, signal = _start(
        tmp_path,
        "hold",
        owner="owner-corrupt",
        options={"release_path": str(release), "spawn_child": True},
    )
    try:
        _await(signal)
        _finish(process, terminate=True)
        if corruption == "snapshot_checksum":
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE event_transactional_states SET payload_json = ? WHERE namespace = ? AND state_key = ?",
                    (
                        '{"schema_version":"tampered"}',
                        CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
                        SCOPE,
                    ),
                )
                connection.commit()
        else:
            store = SQLiteEventStore(database, initialize=False)
            runtime = EventRuntime(
                store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite"
            )
            previous = store.load_transactional_state(
                CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE, SCOPE
            )
            payload = thaw_canonical_json(previous.payload)
            payload["events"][0]["budget"] = {"turns": -1}
            payload["history_checksum"] = (
                "sha256:"
                + hashlib.sha256(
                    stable_json_dumps(payload["events"]).encode("utf-8")
                ).hexdigest()
            )
            # Recompute both envelope checksums to exercise semantic recovery,
            # rather than only detecting a damaged outer persistence record.
            damaged = TransactionalStateSnapshot.create(
                namespace=previous.namespace,
                key=previous.key,
                revision=previous.revision + 1,
                payload=payload,
            )
            runtime.compare_and_swap_transactional_state(
                damaged,
                expected_revision=previous.revision,
                expected_checksum=previous.checksum,
            )

        blocked, _database, blocked_signal = _start(
            tmp_path,
            "takeover",
            owner="owner-after-corruption",
            options={"clock_offset": 60},
        )
        try:
            payload = _await(blocked_signal)
            assert payload["code"] == (
                "child_event_store_unavailable"
                if corruption == "snapshot_checksum"
                else "child_recovery_corrupt"
            )
            assert blocked.wait(timeout=15) == 0
        finally:
            _finish(blocked)
    finally:
        _finish(process)
