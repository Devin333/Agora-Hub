from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.research.rag.evaluation.golden_corpus_snapshot import (
    GoldenCorpusSnapshotError,
)
from backend.research.rag.evaluation.legacy_dataset_inventory import (
    LegacyDatasetInventory,
    LegacyDatasetInventoryError,
    build_legacy_dataset_inventory,
    load_legacy_dataset_inventory,
    write_legacy_dataset_inventory,
)
from backend.research.rag.evaluation.protocol_common import content_digest

GOLDEN_SET = Path("data/eval/golden_set.json")
SNAPSHOT = Path("data/eval/golden_corpus_snapshot.json")
INVENTORY = Path("data/eval/legacy_audit_inventory.json")
EXPECTED_GOLDEN_SHA256 = (
    "083d3e9d0ab38076884a219a3d18025b47ec662bdd95152be7fbca82c3487e37"
)


def test_duplicate_questions_keep_distinct_original_indexes_and_stable_ids(
    tmp_path: Path,
) -> None:
    rows = _fake_rows(3, duplicate_questions=True)
    golden_set, snapshot = _write_valid_fixture(tmp_path, rows)

    first = build_legacy_dataset_inventory(
        golden_set_path=golden_set,
        snapshot_path=snapshot,
    )
    second_root = tmp_path / "other-machine-root"
    second_golden, second_snapshot = _write_valid_fixture(second_root, rows)
    second = build_legacy_dataset_inventory(
        golden_set_path=second_golden,
        snapshot_path=second_snapshot,
    )

    assert [case.original_index for case in first.cases] == [0, 1, 2]
    assert len({case.stable_id for case in first.cases}) == 3
    assert first.cases[0].question == first.cases[1].question
    assert first.cases[0].original_record_digest == first.cases[1].original_record_digest
    assert first.cases[0].stable_id != first.cases[1].stable_id
    assert first.inventory_digest == second.inventory_digest
    assert [case.stable_id for case in first.cases] == [
        case.stable_id for case in second.cases
    ]
    serialized = json.dumps(first.model_dump(mode="json", by_alias=True))
    assert str(tmp_path) not in serialized


def test_inventory_rejects_wrong_snapshot_checksum(tmp_path: Path) -> None:
    golden_set, snapshot = _write_valid_fixture(tmp_path, _fake_rows(2))
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["snapshot_checksum"] = "sha256:" + "0" * 64
    snapshot.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        GoldenCorpusSnapshotError,
        match="golden corpus snapshot checksum mismatch",
    ):
        build_legacy_dataset_inventory(
            golden_set_path=golden_set,
            snapshot_path=snapshot,
        )


def test_batches_cover_input_once_without_mutation_or_false_qualification(
    tmp_path: Path,
) -> None:
    golden_set, snapshot = _write_valid_fixture(tmp_path, _fake_rows(41))
    golden_before = golden_set.read_bytes()
    snapshot_before = snapshot.read_bytes()

    inventory = build_legacy_dataset_inventory(
        golden_set_path=golden_set,
        snapshot_path=snapshot,
    )
    output = tmp_path / "inventory.json"
    write_legacy_dataset_inventory(
        inventory,
        output,
        golden_set_path=golden_set,
        snapshot_path=snapshot,
    )

    batch_indexes = [
        index for batch in inventory.audit_batches for index in batch.original_indexes
    ]
    assert [batch.case_count for batch in inventory.audit_batches] == [20, 20, 1, 0]
    assert len(batch_indexes) == len(set(batch_indexes)) == 41
    assert set(batch_indexes) == set(range(41))
    assert inventory.audit_plan_status.batches_are_disjoint is True
    assert inventory.audit_plan_status.batch_union_matches_all_cases is True
    assert inventory.audit_plan_status.legacy_79_delivery_satisfied is False
    assert inventory.audit_plan_status.delivery_gap_reason is not None
    assert inventory.qualification_counts.reviewed_count == 0
    assert inventory.qualification_counts.clean_exposure_count == 0
    assert inventory.qualification_counts.heldout_count == 0
    assert all(case.dataset_role == "legacy_dev_candidate" for case in inventory.cases)
    assert all(case.audit_status == "needs_review" for case in inventory.cases)
    assert all(case.exposure_status == "unknown" for case in inventory.cases)
    assert all(case.clean_exposure_qualified is False for case in inventory.cases)
    assert all(case.heldout_qualified is False for case in inventory.cases)
    assert all(case.human_review is None for case in inventory.cases)
    assert all(case.reference_facts is None for case in inventory.cases)
    assert all(case.source_version.value is None for case in inventory.cases)
    assert all(case.license_reference.value is None for case in inventory.cases)
    assert golden_set.read_bytes() == golden_before
    assert snapshot.read_bytes() == snapshot_before
    assert output.is_file()


def test_writer_refuses_to_overwrite_read_only_sources(tmp_path: Path) -> None:
    golden_set, snapshot = _write_valid_fixture(tmp_path, _fake_rows(2))
    inventory = build_legacy_dataset_inventory(
        golden_set_path=golden_set,
        snapshot_path=snapshot,
    )
    golden_before = golden_set.read_bytes()
    snapshot_before = snapshot.read_bytes()

    for protected_target in (golden_set, snapshot):
        with pytest.raises(
            LegacyDatasetInventoryError,
            match="must not overwrite the golden set or snapshot input",
        ):
            write_legacy_dataset_inventory(
                inventory,
                protected_target,
                golden_set_path=golden_set,
                snapshot_path=snapshot,
            )

    assert golden_set.read_bytes() == golden_before
    assert snapshot.read_bytes() == snapshot_before


def test_more_than_four_batches_is_rejected_explicitly(tmp_path: Path) -> None:
    golden_set = tmp_path / "golden_set.json"
    golden_set.parent.mkdir(parents=True, exist_ok=True)
    golden_set.write_text(json.dumps(_fake_rows(81)), encoding="utf-8")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")

    with pytest.raises(
        LegacyDatasetInventoryError,
        match="cannot place 81 cases into exactly four batches of at most 20",
    ):
        build_legacy_dataset_inventory(
            golden_set_path=golden_set,
            snapshot_path=snapshot,
        )


def test_strict_contract_rejects_wrong_overall_digest() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    payload["inventory_digest"] = "0" * 64

    with pytest.raises(ValueError, match="inventory_digest does not match"):
        LegacyDatasetInventory.model_validate_json(json.dumps(payload))


def test_strict_contract_rejects_resealed_batch_member_mismatch() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    payload["audit_batches"][0]["original_indexes"][0] = 1
    _reseal(payload)

    with pytest.raises(ValueError, match="members do not match fixed source-order plan"):
        LegacyDatasetInventory.model_validate_json(json.dumps(payload))


def test_strict_contract_rejects_resealed_count_mismatch() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    payload["existing_counts"]["question_count"] = 78
    _reseal(payload)

    with pytest.raises(ValueError, match="existing_counts do not match"):
        LegacyDatasetInventory.model_validate_json(json.dumps(payload))


def test_writer_revalidates_model_copy_and_source_row_binding(tmp_path: Path) -> None:
    golden_set, snapshot = _write_valid_fixture(tmp_path, _fake_rows(2))
    inventory = build_legacy_dataset_inventory(
        golden_set_path=golden_set,
        snapshot_path=snapshot,
    )
    bypassed = inventory.model_copy(update={"inventory_digest": "0" * 64})

    with pytest.raises(ValueError, match="inventory_digest does not match"):
        write_legacy_dataset_inventory(
            bypassed,
            tmp_path / "bypassed.json",
            golden_set_path=golden_set,
            snapshot_path=snapshot,
        )

    forged_payload = inventory.model_dump(mode="json", by_alias=True)
    forged_payload["cases"][0]["original_record_digest"] = "f" * 64
    stable_suffix = content_digest(
        {"original_index": 0, "original_record_digest": "f" * 64}
    )[:16]
    forged_payload["cases"][0]["stable_id"] = f"legacy-case-0000-{stable_suffix}"
    forged_payload["audit_batches"][0]["stable_ids"][0] = (
        forged_payload["cases"][0]["stable_id"]
    )
    _reseal(forged_payload)
    forged = LegacyDatasetInventory.model_validate_json(json.dumps(forged_payload))
    with pytest.raises(
        LegacyDatasetInventoryError,
        match="does not match its source golden-set row",
    ):
        write_legacy_dataset_inventory(
            forged,
            tmp_path / "row-binding-mismatch.json",
            golden_set_path=golden_set,
            snapshot_path=snapshot,
        )

    changed_rows = json.loads(golden_set.read_text(encoding="utf-8"))
    changed_rows[0]["question"] = "Changed after inventory construction"
    golden_set, snapshot = _write_valid_fixture(tmp_path / "changed", changed_rows)
    with pytest.raises(
        LegacyDatasetInventoryError,
        match="source file identities do not match current source bytes",
    ):
        write_legacy_dataset_inventory(
            inventory,
            tmp_path / "source-mismatch.json",
            golden_set_path=golden_set,
            snapshot_path=snapshot,
        )


def test_repository_legacy_inventory_matches_real_sources() -> None:
    golden_before = GOLDEN_SET.read_bytes()
    snapshot_before = SNAPSHOT.read_bytes()
    committed = json.loads(INVENTORY.read_text(encoding="utf-8"))

    inventory = build_legacy_dataset_inventory()
    loaded = load_legacy_dataset_inventory()
    rebuilt = inventory.model_dump(mode="json", by_alias=True)

    assert loaded == inventory

    assert committed == rebuilt
    identity_payload = dict(rebuilt)
    inventory_digest = identity_payload.pop("inventory_digest")
    assert inventory_digest == content_digest(identity_payload)
    assert rebuilt["schema"] == "newsroom.legacy-dataset-inventory/v1"
    assert rebuilt["source_files"][0]["sha256"] == EXPECTED_GOLDEN_SHA256
    assert inventory.existing_counts.question_count == 79
    assert inventory.existing_counts.paper_count == 20
    assert inventory.existing_counts.expected_behavior_counts == {
        "abstain": 12,
        "answer": 67,
    }
    assert inventory.existing_counts.existing_case_id_count == 0
    assert inventory.existing_counts.existing_answer_facts_count == 0
    assert inventory.snapshot.source_document_count == 20
    assert inventory.snapshot.gold_chunk_count == 35
    assert inventory.snapshot.material_scope == "gold_chunks_only"
    assert inventory.snapshot.full_corpus_material_status == "unconfirmed"
    assert [batch.case_count for batch in inventory.audit_batches] == [20, 20, 20, 19]
    assert inventory.audit_plan_status.legacy_79_delivery_satisfied is True
    assert inventory.qualification_counts.legacy_dev_candidate_count == 79
    assert inventory.qualification_counts.needs_review_count == 79
    assert sum(paper.question_count for paper in inventory.papers) == 79
    assert all(paper.snapshot_source_document_present for paper in inventory.papers)
    assert all(paper.full_corpus_material_status == "unconfirmed" for paper in inventory.papers)
    assert GOLDEN_SET.read_bytes() == golden_before
    assert SNAPSHOT.read_bytes() == snapshot_before


def _reseal(payload: dict[str, object]) -> None:
    identity_payload = dict(payload)
    identity_payload.pop("inventory_digest", None)
    payload["inventory_digest"] = content_digest(identity_payload)


def _fake_rows(
    count: int,
    *,
    duplicate_questions: bool = False,
) -> list[dict[str, object]]:
    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    chunk = payload["chunks"][0]
    question = "Which evidence is present in this source?"
    return [
        {
            "question": question if duplicate_questions else f"{question} #{index}",
            "source_chunk_id": chunk["chunk_id"],
            "paper_id": chunk["paper_id"],
            "domain": "fixture",
            "expected_behavior": "answer",
        }
        for index in range(count)
    ]


def _write_valid_fixture(
    root: Path,
    rows: list[dict[str, object]],
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    golden_set = root / "golden_set.json"
    golden_set.write_text(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    repository_snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    chunk_id = str(rows[0]["source_chunk_id"])
    chunk = next(
        item for item in repository_snapshot["chunks"] if item["chunk_id"] == chunk_id
    )
    source = next(
        item
        for item in repository_snapshot["source_documents"]
        if item["paper_id"] == chunk["paper_id"]
    )
    payload = {
        "schema": repository_snapshot["schema"],
        "golden_set_checksum": f"sha256:{hashlib.sha256(golden_set.read_bytes()).hexdigest()}",
        "source_documents": [source],
        "chunks": [chunk],
    }
    payload["snapshot_checksum"] = _snapshot_checksum(payload)
    snapshot = root / "golden_corpus_snapshot.json"
    snapshot.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return golden_set, snapshot


def _snapshot_checksum(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
