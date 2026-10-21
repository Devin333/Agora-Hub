"""Deterministic, read-only inventory for the legacy Research RAG golden set.

This module deliberately inventories existing evidence without upgrading it into
reviewed data.  In particular, a valid gold-chunk snapshot proves only that the
committed regression evidence is internally consistent; it does not prove that
full source material, reference facts, human review, or clean exposure history
exists.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal, Mapping, Self, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.research.rag.evaluation.golden_corpus_snapshot import (
    DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
    load_golden_corpus_snapshot,
)
from backend.research.rag.evaluation.protocol_common import (
    canonical_json_bytes,
    content_digest,
)

DEFAULT_LEGACY_GOLDEN_SET_PATH = Path("data/eval/golden_set.json")
DEFAULT_LEGACY_AUDIT_INVENTORY_PATH = Path("data/eval/legacy_audit_inventory.json")
_inventory_schema = "newsroom.legacy-dataset-inventory/v1"
_dataset_role = "legacy_dev_candidate"
_audit_status = "needs_review"
_exposure_status = "unknown"
_target_case_count = 79
_audit_batch_count = 4
_audit_batch_max_size = 20


class LegacyDatasetInventoryError(ValueError):
    """Raised when source data cannot satisfy the inventory contract."""


class _InventoryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, populate_by_name=True
    )


class SourceFileIdentity(_InventoryModel):
    logical_role: Literal["legacy_golden_set", "gold_chunk_snapshot"]
    repository_path: str
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExistingCounts(_InventoryModel):
    question_count: int = Field(ge=0)
    paper_count: int = Field(ge=0)
    expected_behavior_counts: dict[str, int]
    field_presence_counts: dict[str, int]
    metadata_field_presence_counts: dict[str, int]
    existing_case_id_count: int = Field(ge=0)
    existing_answer_facts_count: int = Field(ge=0)
    existing_exposure_history_count: int = Field(ge=0)
    existing_human_review_count: int = Field(ge=0)
    existing_source_version_count: int = Field(ge=0)
    existing_license_reference_count: int = Field(ge=0)


class SnapshotSummary(_InventoryModel):
    snapshot_schema: str = Field(alias="schema")
    golden_set_checksum: str
    snapshot_checksum: str
    source_document_count: int = Field(ge=0)
    gold_chunk_count: int = Field(ge=0)
    material_scope: Literal["gold_chunks_only"] = "gold_chunks_only"
    full_corpus_material_status: Literal["unconfirmed"] = "unconfirmed"
    full_corpus_material_reason: str


class LegacyIdentifiers(_InventoryModel):
    source_chunk_id: str | None
    gold_chunk_ids: tuple[str, ...]
    gold_source_locators: tuple[str, ...]
    metadata_legacy_source_chunk_id: str | None


class SourceFieldGap(_InventoryModel):
    value: None = None
    reason: str


class LegacyCaseInventory(_InventoryModel):
    original_index: int = Field(ge=0)
    source_record_pointer: str
    stable_id: str
    original_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_fields: tuple[str, ...]
    original_metadata_fields: tuple[str, ...]
    paper_id: str
    question: str
    domain: str
    expected_behavior: str
    qa_type: str | None
    difficulty: str | None
    legacy_identifiers: LegacyIdentifiers
    audit_batch_id: str
    dataset_role: Literal["legacy_dev_candidate"] = _dataset_role
    audit_status: Literal["needs_review"] = _audit_status
    exposure_status: Literal["unknown"] = _exposure_status
    clean_exposure_qualified: Literal[False] = False
    heldout_qualified: Literal[False] = False
    human_review: None = None
    reference_facts: None = None
    source_version: SourceFieldGap
    license_reference: SourceFieldGap
    review_gaps: tuple[str, ...]


class AuditBatch(_InventoryModel):
    batch_id: str
    ordinal: int = Field(ge=1, le=_audit_batch_count)
    max_size: Literal[20] = _audit_batch_max_size
    case_count: int = Field(ge=0, le=_audit_batch_max_size)
    original_indexes: tuple[int, ...]
    stable_ids: tuple[str, ...]


class PaperInventory(_InventoryModel):
    paper_id: str
    question_count: int = Field(ge=1)
    answer_count: int = Field(ge=0)
    abstain_count: int = Field(ge=0)
    original_indexes: tuple[int, ...]
    snapshot_source_document_present: bool
    snapshot_gold_chunk_count: int = Field(ge=0)
    snapshot_source_hash: str | None
    snapshot_document_checksum: str | None
    snapshot_document_checksum_scope: str | None
    snapshot_source_refs: tuple[str, ...]
    source_version: SourceFieldGap
    license_reference: SourceFieldGap
    full_corpus_material_status: Literal["unconfirmed"] = "unconfirmed"
    full_corpus_material_reason: str


class AuditPlanStatus(_InventoryModel):
    required_target_case_count: Literal[79] = _target_case_count
    required_batch_count: Literal[4] = _audit_batch_count
    max_batch_size: Literal[20] = _audit_batch_max_size
    actual_case_count: int = Field(ge=0)
    batches_are_disjoint: bool
    batch_union_matches_all_cases: bool
    legacy_79_delivery_satisfied: bool
    delivery_gap_reason: str | None


class QualificationCounts(_InventoryModel):
    legacy_dev_candidate_count: int = Field(ge=0)
    needs_review_count: int = Field(ge=0)
    reviewed_count: Literal[0] = 0
    clean_exposure_count: Literal[0] = 0
    heldout_count: Literal[0] = 0


class LegacyDatasetInventory(_InventoryModel):
    inventory_schema: Literal["newsroom.legacy-dataset-inventory/v1"] = Field(
        default=_inventory_schema, alias="schema"
    )
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_basis: str
    original_index_base: Literal[0] = 0
    source_files: tuple[SourceFileIdentity, SourceFileIdentity]
    existing_counts: ExistingCounts
    snapshot: SnapshotSummary
    audit_plan_status: AuditPlanStatus
    qualification_counts: QualificationCounts
    audit_batches: tuple[AuditBatch, AuditBatch, AuditBatch, AuditBatch]
    papers: tuple[PaperInventory, ...]
    cases: tuple[LegacyCaseInventory, ...]
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_identity_and_claims(self) -> Self:
        return self.verify_identity()

    def verify_identity(self) -> Self:
        """Reject a structurally inconsistent or incorrectly sealed inventory."""

        _verify_inventory_identity(self)
        return self

    def verify_sources(
        self,
        *,
        golden_set_path: str | Path = DEFAULT_LEGACY_GOLDEN_SET_PATH,
        snapshot_path: str | Path = DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
    ) -> Self:
        """Bind every inventoried case and source claim back to read-only inputs."""

        _verify_inventory_sources(
            self,
            golden_set_path=Path(golden_set_path),
            snapshot_path=Path(snapshot_path),
        )
        return self


def build_legacy_dataset_inventory(
    *,
    golden_set_path: str | Path = DEFAULT_LEGACY_GOLDEN_SET_PATH,
    snapshot_path: str | Path = DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
) -> LegacyDatasetInventory:
    """Build a deterministic inventory without modifying either source file.

    The committed snapshot is loaded through ``load_golden_corpus_snapshot`` so
    all existing byte checksum, schema, chunk-content, and coverage checks remain
    authoritative.  The returned identity contains logical repository paths only;
    caller-specific absolute paths and wall-clock values are intentionally absent.
    """

    golden_path = Path(golden_set_path)
    corpus_snapshot_path = Path(snapshot_path)
    golden_bytes = _read_bytes(golden_path, "legacy golden set")
    snapshot_bytes = _read_bytes(corpus_snapshot_path, "gold chunk snapshot")
    rows = _load_golden_rows(golden_bytes)
    if len(rows) > _audit_batch_count * _audit_batch_max_size:
        raise LegacyDatasetInventoryError(
            "legacy audit inventory cannot place "
            f"{len(rows)} cases into exactly four batches of at most 20"
        )

    snapshot = load_golden_corpus_snapshot(
        corpus_snapshot_path,
        golden_set_path=golden_path,
    )
    if _read_bytes(golden_path, "legacy golden set") != golden_bytes:
        raise LegacyDatasetInventoryError(
            "legacy golden set changed while inventory was being built"
        )
    if _read_bytes(corpus_snapshot_path, "gold chunk snapshot") != snapshot_bytes:
        raise LegacyDatasetInventoryError(
            "gold chunk snapshot changed while inventory was being built"
        )
    expected_golden_checksum = f"sha256:{_sha256(golden_bytes)}"
    if snapshot.golden_set_checksum != expected_golden_checksum:
        raise LegacyDatasetInventoryError(
            "validated snapshot checksum does not match inventoried golden-set bytes"
        )
    source_by_paper = {item.paper_id: item for item in snapshot.source_documents}
    chunks_by_paper = Counter(chunk.paper_id for chunk in snapshot.chunks)
    batch_indexes = _batch_indexes(len(rows))
    batch_id_by_index = {
        index: _batch_id(batch_number)
        for batch_number, indexes in enumerate(batch_indexes, start=1)
        for index in indexes
    }

    cases = tuple(
        _case_inventory(
            row=row,
            original_index=index,
            batch_id=batch_id_by_index[index],
        )
        for index, row in enumerate(rows)
    )
    stable_ids = [case.stable_id for case in cases]
    if len(stable_ids) != len(set(stable_ids)):
        raise LegacyDatasetInventoryError("derived stable case IDs are not unique")

    batches = tuple(
        AuditBatch(
            batch_id=_batch_id(batch_number),
            ordinal=batch_number,
            case_count=len(indexes),
            original_indexes=indexes,
            stable_ids=tuple(cases[index].stable_id for index in indexes),
        )
        for batch_number, indexes in enumerate(batch_indexes, start=1)
    )
    papers = _paper_inventory(
        rows=rows,
        source_by_paper=source_by_paper,
        chunks_by_paper=chunks_by_paper,
    )
    all_batch_indexes = [index for batch in batches for index in batch.original_indexes]
    disjoint = len(all_batch_indexes) == len(set(all_batch_indexes))
    union_matches = set(all_batch_indexes) == set(range(len(rows)))
    delivery_satisfied = (
        len(rows) == _target_case_count and disjoint and union_matches
    )

    payload: dict[str, Any] = {
        "identity_basis": (
            "Canonical JSON content, source-file bytes, original zero-based record "
            "indexes, and deterministic sequential audit batches; excludes absolute "
            "paths, wall-clock time, host identity, and randomness."
        ),
        "original_index_base": 0,
        "source_files": (
            SourceFileIdentity(
                logical_role="legacy_golden_set",
                repository_path=DEFAULT_LEGACY_GOLDEN_SET_PATH.as_posix(),
                byte_count=len(golden_bytes),
                sha256=_sha256(golden_bytes),
            ),
            SourceFileIdentity(
                logical_role="gold_chunk_snapshot",
                repository_path=DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH.as_posix(),
                byte_count=len(snapshot_bytes),
                sha256=_sha256(snapshot_bytes),
            ),
        ),
        "existing_counts": _existing_counts(rows),
        "snapshot": SnapshotSummary(
            schema=snapshot.schema,
            golden_set_checksum=snapshot.golden_set_checksum,
            snapshot_checksum=snapshot.snapshot_checksum,
            source_document_count=len(snapshot.source_documents),
            gold_chunk_count=len(snapshot.chunks),
            full_corpus_material_reason=(
                "The existing snapshot contains only validated gold chunks and source "
                "document identities. It is not a complete retrieval corpus, so full "
                "source material and non-gold distractor coverage remain to be confirmed."
            ),
        ),
        "audit_plan_status": AuditPlanStatus(
            actual_case_count=len(rows),
            batches_are_disjoint=disjoint,
            batch_union_matches_all_cases=union_matches,
            legacy_79_delivery_satisfied=delivery_satisfied,
            delivery_gap_reason=(
                None
                if delivery_satisfied
                else _delivery_gap_reason()
            ),
        ),
        "qualification_counts": QualificationCounts(
            legacy_dev_candidate_count=len(rows),
            needs_review_count=len(rows),
        ),
        "audit_batches": batches,
        "papers": papers,
        "cases": cases,
        "limitations": (
            "No human review is inferred from legacy curation metadata or snapshot validity.",
            "Unknown exposure history is not treated as clean exposure and cannot qualify cases as heldout.",
            "No reference facts are generated; answerability and evidence sufficiency require tasks 3.2-3.7 human audit.",
            "Snapshot source presence proves gold-snapshot provenance only, not complete original material, source version, or license status.",
        ),
    }
    payload["schema"] = _inventory_schema
    inventory_digest = content_digest(_wire_value(payload))
    return LegacyDatasetInventory(inventory_digest=inventory_digest, **payload)


def load_legacy_dataset_inventory(
    path: str | Path = DEFAULT_LEGACY_AUDIT_INVENTORY_PATH,
    *,
    golden_set_path: str | Path = DEFAULT_LEGACY_GOLDEN_SET_PATH,
    snapshot_path: str | Path = DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
) -> LegacyDatasetInventory:
    """Load a sealed inventory and bind it to the current read-only sources."""

    inventory_path = Path(path)
    try:
        inventory = LegacyDatasetInventory.model_validate_json(
            inventory_path.read_bytes()
        )
    except OSError as exc:
        raise LegacyDatasetInventoryError(
            f"cannot read legacy inventory: {type(exc).__name__}"
        ) from exc
    inventory.verify_sources(
        golden_set_path=golden_set_path,
        snapshot_path=snapshot_path,
    )
    return inventory


def write_legacy_dataset_inventory(
    inventory: LegacyDatasetInventory,
    output_path: str | Path = DEFAULT_LEGACY_AUDIT_INVENTORY_PATH,
    *,
    golden_set_path: str | Path = DEFAULT_LEGACY_GOLDEN_SET_PATH,
    snapshot_path: str | Path = DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
) -> None:
    """Write an inventory while refusing either read-only source as the target."""

    validated = LegacyDatasetInventory.model_validate(
        inventory.model_dump(mode="python", by_alias=True)
    )
    validated.verify_sources(
        golden_set_path=golden_set_path,
        snapshot_path=snapshot_path,
    )
    output = Path(output_path)
    protected_sources = (Path(golden_set_path), Path(snapshot_path))
    if any(_same_path(output, source) for source in protected_sources):
        raise LegacyDatasetInventoryError(
            "inventory output path must not overwrite the golden set or snapshot input"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(
        canonical_json_bytes(validated.model_dump(mode="json", by_alias=True)) + b"\n"
    )


def _case_inventory(
    *, row: Mapping[str, Any], original_index: int, batch_id: str
) -> LegacyCaseInventory:
    record_digest = content_digest(row)
    stable_id = _stable_case_id(original_index, record_digest)
    metadata = row.get("metadata")
    metadata_mapping = metadata if isinstance(metadata, Mapping) else {}
    return LegacyCaseInventory(
        original_index=original_index,
        source_record_pointer=f"golden_set.json#/{original_index}",
        stable_id=stable_id,
        original_record_digest=record_digest,
        original_fields=tuple(sorted(row)),
        original_metadata_fields=tuple(sorted(metadata_mapping)),
        paper_id=_required_text(row, "paper_id", original_index),
        question=_required_text(row, "question", original_index),
        domain=_required_text(row, "domain", original_index),
        expected_behavior=_required_text(row, "expected_behavior", original_index),
        qa_type=_optional_text(row.get("qa_type"), "qa_type", original_index),
        difficulty=_optional_text(row.get("difficulty"), "difficulty", original_index),
        legacy_identifiers=LegacyIdentifiers(
            source_chunk_id=_optional_text(
                row.get("source_chunk_id"), "source_chunk_id", original_index
            ),
            gold_chunk_ids=_text_tuple(
                row.get("gold_chunk_ids", ()), "gold_chunk_ids", original_index
            ),
            gold_source_locators=_text_tuple(
                row.get("gold_source_locators", ()),
                "gold_source_locators",
                original_index,
            ),
            metadata_legacy_source_chunk_id=_optional_text(
                metadata_mapping.get("legacy_source_chunk_id"),
                "metadata.legacy_source_chunk_id",
                original_index,
            ),
        ),
        audit_batch_id=batch_id,
        source_version=SourceFieldGap(
            reason="The legacy record does not declare a versioned source revision."
        ),
        license_reference=SourceFieldGap(
            reason="The legacy record and gold-chunk snapshot do not declare a license reference."
        ),
        review_gaps=(
            "case_id_missing",
            "reference_facts_missing",
            "exposure_history_missing",
            "human_review_missing",
            "source_version_missing",
            "license_reference_missing",
            "full_corpus_material_unconfirmed",
        ),
    )


def _paper_inventory(
    *,
    rows: Sequence[Mapping[str, Any]],
    source_by_paper: Mapping[str, Any],
    chunks_by_paper: Mapping[str, int],
) -> tuple[PaperInventory, ...]:
    indexes_by_paper: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        paper_id = _required_text(row, "paper_id", index)
        indexes_by_paper.setdefault(paper_id, []).append(index)

    result: list[PaperInventory] = []
    for paper_id in sorted(indexes_by_paper):
        indexes = tuple(indexes_by_paper[paper_id])
        source = source_by_paper.get(paper_id)
        behaviors = Counter(str(rows[index].get("expected_behavior")) for index in indexes)
        result.append(
            PaperInventory(
                paper_id=paper_id,
                question_count=len(indexes),
                answer_count=behaviors.get("answer", 0),
                abstain_count=behaviors.get("abstain", 0),
                original_indexes=indexes,
                snapshot_source_document_present=source is not None,
                snapshot_gold_chunk_count=int(chunks_by_paper.get(paper_id, 0)),
                snapshot_source_hash=None if source is None else source.source_hash,
                snapshot_document_checksum=(
                    None if source is None else source.document_checksum
                ),
                snapshot_document_checksum_scope=(
                    None if source is None else source.document_checksum_scope
                ),
                snapshot_source_refs=() if source is None else source.source_refs,
                source_version=SourceFieldGap(
                    reason=(
                        "Snapshot source refs identify origin and content, but no explicit "
                        "source revision/version is declared."
                    )
                ),
                license_reference=SourceFieldGap(
                    reason="No license reference is declared by the legacy sources."
                ),
                full_corpus_material_reason=(
                    "Only gold chunks are present in the existing snapshot; complete "
                    "paper material and non-gold corpus content require separate confirmation."
                ),
            )
        )
    return tuple(result)


def _existing_counts(rows: Sequence[Mapping[str, Any]]) -> ExistingCounts:
    field_counts = Counter(key for row in rows for key in row)
    metadata_counts: Counter[str] = Counter()
    for row in rows:
        metadata = row.get("metadata")
        if isinstance(metadata, Mapping):
            metadata_counts.update(str(key) for key in metadata)
    return ExistingCounts(
        question_count=len(rows),
        paper_count=len({str(row.get("paper_id")) for row in rows}),
        expected_behavior_counts=dict(
            sorted(Counter(str(row.get("expected_behavior")) for row in rows).items())
        ),
        field_presence_counts=dict(sorted(field_counts.items())),
        metadata_field_presence_counts=dict(sorted(metadata_counts.items())),
        existing_case_id_count=field_counts.get("case_id", 0),
        existing_answer_facts_count=field_counts.get("answer_facts", 0),
        existing_exposure_history_count=field_counts.get("exposure_history", 0),
        existing_human_review_count=field_counts.get("review", 0),
        existing_source_version_count=field_counts.get("source_version", 0),
        existing_license_reference_count=field_counts.get("license_reference", 0),
    )


def _batch_indexes(case_count: int) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(
            range(
                batch_number * _audit_batch_max_size,
                min((batch_number + 1) * _audit_batch_max_size, case_count),
            )
        )
        for batch_number in range(_audit_batch_count)
    )


def _batch_id(batch_number: int) -> str:
    return f"legacy-audit-batch-{batch_number:02d}"


def _load_golden_rows(data: bytes) -> tuple[Mapping[str, Any], ...]:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyDatasetInventoryError(
            f"invalid legacy golden set JSON: {type(exc).__name__}"
        ) from exc
    if not isinstance(value, list) or not value:
        raise LegacyDatasetInventoryError(
            "legacy golden set must be a non-empty JSON array"
        )
    rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise LegacyDatasetInventoryError(
                f"legacy golden set row {index} must be an object"
            )
        rows.append(row)
    return tuple(rows)


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LegacyDatasetInventoryError(
            f"cannot read {label}: {type(exc).__name__}"
        ) from exc


def _required_text(row: Mapping[str, Any], field: str, index: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LegacyDatasetInventoryError(
            f"legacy golden set row {index} has invalid {field}"
        )
    return value


def _optional_text(value: Any, field: str, index: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LegacyDatasetInventoryError(
            f"legacy golden set row {index} has invalid {field}"
        )
    return value


def _text_tuple(value: Any, field: str, index: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        if value == ():
            return ()
        raise LegacyDatasetInventoryError(
            f"legacy golden set row {index} has invalid {field}"
        )
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LegacyDatasetInventoryError(
                f"legacy golden set row {index} has invalid {field} item"
            )
        result.append(item)
    return tuple(result)


def _verify_inventory_identity(inventory: LegacyDatasetInventory) -> None:
    cases = inventory.cases
    if not cases:
        raise LegacyDatasetInventoryError("inventory must contain at least one case")

    wire_payload = inventory.model_dump(mode="json", by_alias=True)
    actual_digest = wire_payload.pop("inventory_digest")
    expected_digest = content_digest(wire_payload)
    if actual_digest != expected_digest:
        raise LegacyDatasetInventoryError("inventory_digest does not match inventory content")

    expected_roles = ("legacy_golden_set", "gold_chunk_snapshot")
    if tuple(item.logical_role for item in inventory.source_files) != expected_roles:
        raise LegacyDatasetInventoryError("source file roles must be unique and ordered")
    expected_paths = (
        DEFAULT_LEGACY_GOLDEN_SET_PATH.as_posix(),
        DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH.as_posix(),
    )
    if tuple(item.repository_path for item in inventory.source_files) != expected_paths:
        raise LegacyDatasetInventoryError("source file repository paths do not match their roles")
    if inventory.snapshot.golden_set_checksum != (
        f"sha256:{inventory.source_files[0].sha256}"
    ):
        raise LegacyDatasetInventoryError(
            "snapshot golden_set_checksum does not match source file identity"
        )

    indexes = tuple(case.original_index for case in cases)
    if indexes != tuple(range(len(cases))):
        raise LegacyDatasetInventoryError(
            "case original indexes must be unique, contiguous, and source ordered"
        )
    stable_ids = tuple(case.stable_id for case in cases)
    if len(stable_ids) != len(set(stable_ids)):
        raise LegacyDatasetInventoryError("case stable IDs must be unique")
    for case in cases:
        expected_id = _stable_case_id(case.original_index, case.original_record_digest)
        if case.stable_id != expected_id:
            raise LegacyDatasetInventoryError(
                f"case {case.original_index} stable ID does not bind its row digest"
            )
        if case.source_record_pointer != f"golden_set.json#/{case.original_index}":
            raise LegacyDatasetInventoryError(
                f"case {case.original_index} has an invalid source record pointer"
            )
        if case.original_fields != tuple(sorted(set(case.original_fields))):
            raise LegacyDatasetInventoryError(
                f"case {case.original_index} original_fields must be sorted and unique"
            )
        if case.original_metadata_fields != tuple(
            sorted(set(case.original_metadata_fields))
        ):
            raise LegacyDatasetInventoryError(
                f"case {case.original_index} original_metadata_fields must be sorted and unique"
            )

    field_counts = Counter(field for case in cases for field in case.original_fields)
    metadata_counts = Counter(
        field for case in cases for field in case.original_metadata_fields
    )
    expected_behavior_counts = dict(
        sorted(Counter(case.expected_behavior for case in cases).items())
    )
    counts = inventory.existing_counts
    expected_existing_counts = {
        "question_count": len(cases),
        "paper_count": len({case.paper_id for case in cases}),
        "expected_behavior_counts": expected_behavior_counts,
        "field_presence_counts": dict(sorted(field_counts.items())),
        "metadata_field_presence_counts": dict(sorted(metadata_counts.items())),
        "existing_case_id_count": field_counts.get("case_id", 0),
        "existing_answer_facts_count": field_counts.get("answer_facts", 0),
        "existing_exposure_history_count": field_counts.get("exposure_history", 0),
        "existing_human_review_count": field_counts.get("review", 0),
        "existing_source_version_count": field_counts.get("source_version", 0),
        "existing_license_reference_count": field_counts.get("license_reference", 0),
    }
    if counts.model_dump(mode="json") != expected_existing_counts:
        raise LegacyDatasetInventoryError("existing_counts do not match inventoried cases")

    expected_batches = _batch_indexes(len(cases))
    flattened_indexes: list[int] = []
    flattened_ids: list[str] = []
    for ordinal, (batch, expected_indexes) in enumerate(
        zip(inventory.audit_batches, expected_batches, strict=True), start=1
    ):
        if batch.ordinal != ordinal or batch.batch_id != _batch_id(ordinal):
            raise LegacyDatasetInventoryError("audit batch identity or ordinal mismatch")
        expected_ids = tuple(cases[index].stable_id for index in expected_indexes)
        if batch.original_indexes != expected_indexes or batch.stable_ids != expected_ids:
            raise LegacyDatasetInventoryError(
                f"audit batch {batch.batch_id} members do not match fixed source-order plan"
            )
        if batch.case_count != len(expected_indexes):
            raise LegacyDatasetInventoryError(
                f"audit batch {batch.batch_id} case_count does not match members"
            )
        for index in expected_indexes:
            if cases[index].audit_batch_id != batch.batch_id:
                raise LegacyDatasetInventoryError(
                    f"case {index} audit_batch_id does not match batch membership"
                )
        flattened_indexes.extend(batch.original_indexes)
        flattened_ids.extend(batch.stable_ids)

    batches_disjoint = len(flattened_indexes) == len(set(flattened_indexes))
    ids_disjoint = len(flattened_ids) == len(set(flattened_ids))
    union_matches = set(flattened_indexes) == set(range(len(cases)))
    delivery_satisfied = (
        len(cases) == _target_case_count
        and batches_disjoint
        and ids_disjoint
        and union_matches
    )
    plan = inventory.audit_plan_status
    expected_gap = None if delivery_satisfied else _delivery_gap_reason()
    if (
        plan.actual_case_count != len(cases)
        or plan.batches_are_disjoint != (batches_disjoint and ids_disjoint)
        or plan.batch_union_matches_all_cases != union_matches
        or plan.legacy_79_delivery_satisfied != delivery_satisfied
        or plan.delivery_gap_reason != expected_gap
    ):
        raise LegacyDatasetInventoryError("audit_plan_status claims do not match batches")

    qualification = inventory.qualification_counts
    if (
        qualification.legacy_dev_candidate_count != len(cases)
        or qualification.needs_review_count != len(cases)
    ):
        raise LegacyDatasetInventoryError(
            "qualification counts do not match legacy needs-review cases"
        )

    paper_ids = tuple(paper.paper_id for paper in inventory.papers)
    expected_paper_ids = tuple(sorted({case.paper_id for case in cases}))
    if paper_ids != expected_paper_ids:
        raise LegacyDatasetInventoryError("paper inventory must be unique and sorted")
    for paper in inventory.papers:
        paper_cases = tuple(case for case in cases if case.paper_id == paper.paper_id)
        expected_indexes_for_paper = tuple(case.original_index for case in paper_cases)
        behaviors = Counter(case.expected_behavior for case in paper_cases)
        if (
            paper.original_indexes != expected_indexes_for_paper
            or paper.question_count != len(paper_cases)
            or paper.answer_count != behaviors.get("answer", 0)
            or paper.abstain_count != behaviors.get("abstain", 0)
        ):
            raise LegacyDatasetInventoryError(
                f"paper {paper.paper_id} counts do not match inventoried cases"
            )
        if not paper.snapshot_source_document_present:
            raise LegacyDatasetInventoryError(
                f"paper {paper.paper_id} lacks validated snapshot source provenance"
            )
        if (
            paper.snapshot_source_hash is None
            or paper.snapshot_document_checksum is None
            or paper.snapshot_document_checksum_scope is None
            or not paper.snapshot_source_refs
        ):
            raise LegacyDatasetInventoryError(
                f"paper {paper.paper_id} has incomplete snapshot source identity"
            )
    if inventory.snapshot.source_document_count != len(inventory.papers):
        raise LegacyDatasetInventoryError(
            "snapshot source_document_count does not match paper inventory"
        )
    if inventory.snapshot.gold_chunk_count != sum(
        paper.snapshot_gold_chunk_count for paper in inventory.papers
    ):
        raise LegacyDatasetInventoryError(
            "snapshot gold_chunk_count does not match paper inventory"
        )


def _verify_inventory_sources(
    inventory: LegacyDatasetInventory,
    *,
    golden_set_path: Path,
    snapshot_path: Path,
) -> None:
    inventory.verify_identity()
    golden_bytes = _read_bytes(golden_set_path, "legacy golden set")
    snapshot_bytes = _read_bytes(snapshot_path, "gold chunk snapshot")
    expected_source_files = (
        SourceFileIdentity(
            logical_role="legacy_golden_set",
            repository_path=DEFAULT_LEGACY_GOLDEN_SET_PATH.as_posix(),
            byte_count=len(golden_bytes),
            sha256=_sha256(golden_bytes),
        ),
        SourceFileIdentity(
            logical_role="gold_chunk_snapshot",
            repository_path=DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH.as_posix(),
            byte_count=len(snapshot_bytes),
            sha256=_sha256(snapshot_bytes),
        ),
    )
    if inventory.source_files != expected_source_files:
        raise LegacyDatasetInventoryError(
            "inventory source file identities do not match current source bytes"
        )

    rows = _load_golden_rows(golden_bytes)
    snapshot = load_golden_corpus_snapshot(
        snapshot_path,
        golden_set_path=golden_set_path,
    )
    if len(rows) != len(inventory.cases):
        raise LegacyDatasetInventoryError(
            "inventory case count does not match current golden set"
        )
    for index, (row, case) in enumerate(zip(rows, inventory.cases, strict=True)):
        expected = _case_inventory(
            row=row,
            original_index=index,
            batch_id=_batch_id(index // _audit_batch_max_size + 1),
        )
        if case != expected:
            raise LegacyDatasetInventoryError(
                f"inventory case {index} does not match its source golden-set row"
            )

    source_by_paper = {item.paper_id: item for item in snapshot.source_documents}
    chunks_by_paper = Counter(chunk.paper_id for chunk in snapshot.chunks)
    expected_papers = _paper_inventory(
        rows=rows,
        source_by_paper=source_by_paper,
        chunks_by_paper=chunks_by_paper,
    )
    if inventory.papers != expected_papers:
        raise LegacyDatasetInventoryError(
            "paper inventory does not match validated snapshot provenance"
        )
    if (
        inventory.snapshot.snapshot_schema != snapshot.schema
        or inventory.snapshot.golden_set_checksum != snapshot.golden_set_checksum
        or inventory.snapshot.snapshot_checksum != snapshot.snapshot_checksum
        or inventory.snapshot.source_document_count != len(snapshot.source_documents)
        or inventory.snapshot.gold_chunk_count != len(snapshot.chunks)
    ):
        raise LegacyDatasetInventoryError(
            "snapshot summary does not match validated snapshot content"
        )


def _stable_case_id(original_index: int, record_digest: str) -> str:
    stable_suffix = content_digest(
        {"original_index": original_index, "original_record_digest": record_digest}
    )[:16]
    return f"legacy-case-{original_index:04d}-{stable_suffix}"


def _delivery_gap_reason() -> str:
    return (
        "The source does not contain exactly 79 cases satisfying the fixed "
        "four-batch inventory target."
    )


def _wire_value(value: Any) -> Any:
    """Project typed values to their public JSON field names before hashing."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_value(item) for item in value]
    return value


def _same_path(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False



def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
