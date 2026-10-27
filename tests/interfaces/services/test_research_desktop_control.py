from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

from interfaces.services.research_desktop_control import (
    LocalJsonResearchDesktopRequestStore,
    ResearchDesktopControlError,
    canonical_request_fingerprint,
)


def test_canonical_fingerprint_matches_cross_language_fixtures() -> None:
    fixture_path = Path("tests/fixtures/research_desktop_request_contracts.json")
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert [canonical_request_fingerprint(case["semantic"]) for case in payload["cases"]] == [
        case["fingerprint"] for case in payload["cases"]
    ]


def test_admission_is_owner_scoped_idempotent_and_generation_fenced(tmp_path: Path) -> None:
    store = LocalJsonResearchDesktopRequestStore(tmp_path / "control.json")
    generation = store.generation()
    semantic = {"kind": "analyze", "paperId": "2401.01234", "sourceUrl": None}
    fingerprint = canonical_request_fingerprint(semantic)

    first = store.admit(
        owner_id="alice", request_id="request-0001", generation=generation,
        kind="analyze", fingerprint=fingerprint, semantic=semantic,
    )
    duplicate = store.admit(
        owner_id="alice", request_id="request-0001", generation=generation,
        kind="analyze", fingerprint=fingerprint, semantic=semantic,
    )

    assert first.execute is True
    assert duplicate.execute is False
    with pytest.raises(ResearchDesktopControlError) as conflict:
        store.admit(
            owner_id="alice", request_id="request-0001", generation=generation,
            kind="analyze", fingerprint=canonical_request_fingerprint({**semantic, "paperId": "other"}),
            semantic={**semantic, "paperId": "other"},
        )
    assert conflict.value.code == "desktop_request_conflict"
    rotated = store.rotate_generation()
    assert rotated != generation
    with pytest.raises(ResearchDesktopControlError) as stale:
        store.get(owner_id="alice", request_id="request-0001", generation=generation)
    assert stale.value.code == "desktop_generation_stale"


def test_drain_blocks_all_business_admission_and_capability_receipts_are_runtime_and_generation_scoped(
    tmp_path: Path,
) -> None:
    store = LocalJsonResearchDesktopRequestStore(tmp_path / "control.json")
    runtime_identity = "sha256:" + "a" * 64
    changed_release_identity = "sha256:" + "b" * 64
    receipt = store.record_capability_verification(
        capability_id="pdf-import",
        operation="import",
        runtime_identity=runtime_identity,
    )
    assert receipt["verifiedAt"]
    assert receipt["runtimeIdentity"] == runtime_identity
    visible = store.capability_verifications(runtime_identity=runtime_identity)
    # Verifications are shared by every account and name no owner's records.
    assert visible["pdf-import"]["provenance"] == {"verificationKind": "business_operation", "operation": "import"}
    assert store.capability_verifications(runtime_identity=changed_release_identity) == {}

    assert store.begin_drain(timeout_seconds=0.01)["status"] == "drained"
    with pytest.raises(ResearchDesktopControlError) as draining:
        store.begin_untracked_execution(operation="POST /api/v1/research/papers/analyze")
    assert draining.value.code == "desktop_admission_draining"
    store.rotate_generation()
    assert store.capability_verifications(runtime_identity=runtime_identity) == {}
    assert store.resume_admission()["status"] == "accepting"



@pytest.mark.parametrize(
    "timeout_seconds",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, False, 300.01],
)
def test_begin_drain_rejects_invalid_timeout_before_creating_control_state(
    tmp_path: Path,
    timeout_seconds: object,
) -> None:
    path = tmp_path / "control.json"
    store = LocalJsonResearchDesktopRequestStore(path)

    with pytest.raises(ResearchDesktopControlError) as invalid:
        store.begin_drain(timeout_seconds=timeout_seconds)  # type: ignore[arg-type]

    assert invalid.value.code == "desktop_drain_timeout_invalid"
    assert not path.exists()


def test_concurrent_admission_executes_same_owner_request_once(tmp_path: Path) -> None:
    store = LocalJsonResearchDesktopRequestStore(tmp_path / "control.json")
    generation = store.generation()
    semantic = {"kind": "ask", "paperId": "paper-a", "question": "Why?"}
    fingerprint = canonical_request_fingerprint(semantic)
    barrier = Barrier(2)

    def admit() -> bool:
        barrier.wait()
        return store.admit(
            owner_id="alice",
            request_id="request-concurrent",
            generation=generation,
            kind="ask",
            fingerprint=fingerprint,
            semantic=semantic,
        ).execute

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: admit(), range(2)))

    assert sorted(results) == [False, True]


def test_dead_lease_is_not_kept_alive_by_same_request_id_from_another_owner(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    store = LocalJsonResearchDesktopRequestStore(path)
    generation = store.generation()
    semantic = {"kind": "ask", "paperId": "paper-a", "question": "Why?"}
    fingerprint = canonical_request_fingerprint(semantic)
    for owner in ("alice", "bob"):
        assert store.admit(
            owner_id=owner,
            request_id="shared-request-id",
            generation=generation,
            kind="ask",
            fingerprint=fingerprint,
            semantic=semantic,
        ).execute is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    alice_key = next(
        key for key, value in payload["operations"].items() if value["ownerId"] == "alice"
    )
    payload["activeLeases"][alice_key]["processIdentity"] = "procfs-starttime:dead"
    path.write_text(json.dumps(payload), encoding="utf-8")

    alice = store.get(
        owner_id="alice", request_id="shared-request-id", generation=generation
    )
    bob = store.get(owner_id="bob", request_id="shared-request-id", generation=generation)

    assert alice is not None and alice.status == "needs_review"
    assert bob is not None and bob.status == "running"
