from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from framework.agent.artifacts.paths import resolve_artifact_descendant
from framework.agent.artifacts.stores.fs_safety import (
    is_link_or_reparse_point,
    reject_link_chain,
    verified_atomic_write,
)
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.events.canonical import checksum_for
from framework.harness.artifacts.ports import ArtifactReferenceDescriptor
from framework.harness.artifacts.catalog import (
    ArtifactCatalogClaim,
    ArtifactCatalogEntry,
    ArtifactCatalogGcAction,
    ArtifactCatalogGcDecision,
    ArtifactCatalogGcDetachReceipt,
    ArtifactCatalogGcDetachRequest,
    ArtifactCatalogGcPlan,
    ArtifactCatalogGcReason,
    ArtifactCatalogReconciliationIssue,
    ArtifactCatalogReconciliationIssueKind,
    ArtifactCatalogReconciliationPlan,
    ArtifactCatalogRegistrationRequest,
    ArtifactCatalogRegistrationResult,
    ArtifactCatalogSnapshot,
    ArtifactLifecycleAuthorityKind,
    ArtifactLogicalReference,
    ArtifactReferenceKind,
    ArtifactReferenceRetirementReceipt,
    ArtifactReferenceRetirementRequest,
    ArtifactVerificationReceipt,
)
from framework.harness.runtime.result_canonical import (
    aware_datetime,
    checksum,
    exact_reference,
    identifier,
    media_type as validate_media_type,
    reference,
)
from framework.harness.runtime.result_errors import (
    GraphArtifactResultError,
    GraphArtifactResultErrorCode,
    result_error,
)
from framework.shared.json import stable_json_dumps


CATALOG_SCHEMA_VERSION = "newsroom.graph-artifact-catalog/v1"
DEFAULT_MAX_CATALOG_ENTRIES = 100_000
DEFAULT_MAX_CATALOG_CLAIMS = 1_000_000
DEFAULT_MAX_CATALOG_REFERENCES = 1_000_000
DEFAULT_MAX_CATALOG_STATE_BYTES = 128 * 1024 * 1024

_STATE_FILE_NAME = "catalog.json"
_LOCK_FILE_NAME = "catalog.lock"
_STATE_FIELDS = frozenset(
    {"schema_version", "entries", "claims", "references", "state_checksum"}
)
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _CatalogState:
    entries: Mapping[str, ArtifactCatalogEntry]
    claims: Mapping[str, ArtifactCatalogClaim]
    references: Mapping[str, ArtifactLogicalReference]
    issues: tuple[ArtifactCatalogReconciliationIssue, ...] = ()

    @classmethod
    def empty(cls) -> _CatalogState:
        return cls(entries={}, claims={}, references={})

    def payload_without_checksum(self) -> dict[str, Any]:
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "entries": [
                self.entries[key].to_dict() for key in sorted(self.entries)
            ],
            "claims": [self.claims[key].to_dict() for key in sorted(self.claims)],
            "references": [
                self.references[key].to_dict() for key in sorted(self.references)
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload_without_checksum()
        return {**payload, "state_checksum": checksum_for(payload)}


class LocalJsonArtifactCatalog:
    """Restart-safe metadata catalog for verified Harness artifacts."""

    def __init__(
        self,
        root: str | Path = ".newsroom/runs/_records/graph_artifact_catalog",
        *,
        max_entries: int = DEFAULT_MAX_CATALOG_ENTRIES,
        max_claims: int = DEFAULT_MAX_CATALOG_CLAIMS,
        max_references: int = DEFAULT_MAX_CATALOG_REFERENCES,
        max_state_bytes: int = DEFAULT_MAX_CATALOG_STATE_BYTES,
    ) -> None:
        self.root = Path(root)
        self.max_entries = _positive_bound(max_entries, "max_entries")
        self.max_claims = _positive_bound(max_claims, "max_claims")
        self.max_references = _positive_bound(max_references, "max_references")
        self.max_state_bytes = _positive_bound(max_state_bytes, "max_state_bytes")
        self._state_path = resolve_artifact_descendant(
            self.root,
            _STATE_FILE_NAME,
            field="graph artifact catalog state",
        )
        self._lock_path = resolve_artifact_descendant(
            self.root,
            _LOCK_FILE_NAME,
            field="graph artifact catalog lock",
        )
        self._thread_lock = _path_lock(self._lock_path)

    def register(
        self,
        request: ArtifactCatalogRegistrationRequest,
    ) -> ArtifactCatalogRegistrationResult:
        if not isinstance(request, ArtifactCatalogRegistrationRequest):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="catalog.registration",
            )
        candidate = ArtifactCatalogEntry.from_verified_record(
            request.record,
            request.verification,
        )
        candidate_claim = ArtifactCatalogClaim.for_record(
            request.record,
            entry_id=candidate.entry_id,
        )

        def mutate(state: _CatalogState) -> tuple[_CatalogState, ArtifactCatalogRegistrationResult]:
            existing_claim = state.claims.get(candidate_claim.claim_id)
            if existing_claim is not None and existing_claim.entry_id != candidate.entry_id:
                raise result_error(
                    GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT,
                    field="catalog.claim",
                )

            existing_entry = state.entries.get(candidate.entry_id)
            if existing_entry is not None:
                _validate_dedup_candidate(existing_entry, candidate)
                canonical_entry = existing_entry
                deduplicated = True
            else:
                if len(state.entries) >= self.max_entries:
                    raise result_error(
                        GraphArtifactResultErrorCode.ARTIFACT_QUOTA_EXCEEDED,
                        field="catalog.entries",
                        limit=self.max_entries,
                    )
                canonical_entry = candidate
                deduplicated = False

            claim = ArtifactCatalogClaim.for_record(
                request.record,
                entry_id=canonical_entry.entry_id,
                canonical_ref=canonical_entry.record.ref,
            )
            if existing_claim is not None and existing_claim != claim:
                raise result_error(
                    GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT,
                    field="catalog.claim",
                )

            if existing_claim is not None:
                canonical_claim = existing_claim
            else:
                if len(state.claims) >= self.max_claims:
                    raise result_error(
                        GraphArtifactResultErrorCode.ARTIFACT_QUOTA_EXCEEDED,
                        field="catalog.claims",
                        limit=self.max_claims,
                    )
                canonical_claim = claim

            existing_reference = state.references.get(request.initial_reference.reference_id)
            if existing_reference is not None:
                if existing_reference != request.initial_reference:
                    raise result_error(
                        GraphArtifactResultErrorCode.ARTIFACT_REFERENCE_CONFLICT,
                        field="catalog.reference",
                    )
                canonical_reference = existing_reference
            else:
                if len(state.references) >= self.max_references:
                    raise result_error(
                        GraphArtifactResultErrorCode.ARTIFACT_QUOTA_EXCEEDED,
                        field="catalog.references",
                        limit=self.max_references,
                    )
                canonical_reference = request.initial_reference

            entries = dict(state.entries)
            entries[canonical_entry.entry_id] = canonical_entry
            claims = dict(state.claims)
            claims[canonical_claim.claim_id] = canonical_claim
            references = dict(state.references)
            references[canonical_reference.reference_id] = canonical_reference
            updated = _CatalogState(entries=entries, claims=claims, references=references)
            return updated, ArtifactCatalogRegistrationResult(
                entry=canonical_entry,
                claim=canonical_claim,
                reference=canonical_reference,
                deduplicated=deduplicated,
            )

        return self._mutate(mutate)

    def get(self, entry_id: str) -> ArtifactCatalogEntry:
        normalized = reference(entry_id, "catalog.entry_id")
        state = self._read_snapshot()
        try:
            return state.entries[normalized]
        except KeyError as exc:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND,
                field="catalog.entry_id",
            ) from exc

    def get_by_ref(
        self,
        *,
        tenant_id: str,
        ref: str,
    ) -> ArtifactCatalogEntry:
        tenant = identifier(tenant_id, "catalog.tenant_id")
        physical_ref = reference(ref, "catalog.ref")
        matches = tuple(
            entry
            for entry in self._read_snapshot().entries.values()
            if entry.identity.tenant_id == tenant
            and entry.record.ref == physical_ref
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND,
                field="catalog.ref",
            )
        raise result_error(
            GraphArtifactResultErrorCode.ARTIFACT_CATALOG_CORRUPT,
            field="catalog.ref",
        )

    def describe_artifact_ref(
        self,
        ref: str,
        *,
        expected_run_id: str,
        expected_tenant_id: str | None = None,
    ) -> ArtifactReferenceDescriptor:
        """Describe a physical ref from the checksum-protected catalog state."""

        if expected_tenant_id is None:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH,
                field="catalog.expected_tenant_id",
            )
        tenant = identifier(expected_tenant_id, "catalog.expected_tenant_id")
        expected_run = identifier(expected_run_id, "catalog.expected_run_id")
        entry = self.get_by_ref(tenant_id=tenant, ref=ref)
        record = entry.record
        if record.run_id != expected_run:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH,
                field="catalog.expected_run_id",
            )
        return ArtifactReferenceDescriptor(
            ref=record.ref,
            run_id=record.run_id,
            artifact_type=record.artifact_type,
            checksum=record.content_checksum,
            byte_size=record.byte_size,
            media_type=record.media_type,
            tenant_id=record.tenant_id,
            graph_id=record.graph_id,
            node_id=record.node_id,
            attempt_id=record.attempt_id,
        )

    def get_claim(
        self,
        *,
        tenant_id: str,
        run_id: str,
        artifact_id: str,
    ) -> ArtifactCatalogClaim:
        claim_id = ArtifactCatalogClaim.claim_id_for(
            tenant_id=tenant_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        state = self._read_snapshot()
        try:
            return state.claims[claim_id]
        except KeyError as exc:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND,
                field="catalog.claim_id",
            ) from exc

    def find_by_checksum(
        self,
        *,
        tenant_id: str,
        content_checksum: str,
        media_type: str | None = None,
        producer_revision: str | None = None,
    ) -> tuple[ArtifactCatalogEntry, ...]:
        tenant = identifier(tenant_id, "catalog.tenant_id")
        content = checksum(content_checksum, "catalog.content_checksum")
        normalized_media_type = (
            validate_media_type(media_type, "catalog.media_type")
            if media_type is not None
            else None
        )
        revision = (
            exact_reference(producer_revision, "catalog.producer_revision")
            if producer_revision is not None
            else None
        )
        matches = (
            entry
            for entry in self._read_snapshot().entries.values()
            if entry.identity.tenant_id == tenant
            and entry.identity.content_checksum == content
            and (normalized_media_type is None or entry.identity.media_type == normalized_media_type)
            and (revision is None or entry.identity.producer_revision == revision)
        )
        return tuple(sorted(matches, key=lambda item: item.entry_id))

    def list_by_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> tuple[ArtifactCatalogEntry, ...]:
        tenant = identifier(tenant_id, "catalog.tenant_id")
        run = identifier(run_id, "catalog.run_id")
        state = self._read_snapshot()
        entry_ids = {
            claim.entry_id
            for claim in state.claims.values()
            if claim.tenant_id == tenant and claim.run_id == run
        }
        entry_ids.update(
            item.entry_id
            for item in state.references.values()
            if item.tenant_id == tenant and item.owner_run_id == run
        )
        return tuple(
            state.entries[entry_id]
            for entry_id in sorted(entry_ids)
            if entry_id in state.entries
        )

    def list_references(
        self,
        entry_id: str,
    ) -> tuple[ArtifactLogicalReference, ...]:
        normalized = reference(entry_id, "catalog.entry_id")
        state = self._read_snapshot()
        if normalized not in state.entries:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND,
                field="catalog.entry_id",
            )
        return tuple(
            sorted(
                (
                    item
                    for item in state.references.values()
                    if item.entry_id == normalized
                ),
                key=lambda item: item.reference_id,
            )
        )

    def list_claims_by_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> tuple[ArtifactCatalogClaim, ...]:
        tenant = identifier(tenant_id, "catalog.tenant_id")
        run = identifier(run_id, "catalog.run_id")
        return tuple(
            sorted(
                (
                    claim
                    for claim in self._read_snapshot().claims.values()
                    if claim.tenant_id == tenant and claim.run_id == run
                ),
                key=lambda claim: claim.claim_id,
            )
        )

    def add_reference(
        self,
        logical_reference: ArtifactLogicalReference,
    ) -> ArtifactLogicalReference:
        if not isinstance(logical_reference, ArtifactLogicalReference):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="catalog.reference",
            )

        def mutate(state: _CatalogState) -> tuple[_CatalogState, ArtifactLogicalReference]:
            entry = state.entries.get(logical_reference.entry_id)
            if entry is None:
                raise result_error(
                    GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND,
                    field="catalog.entry_id",
                )
            if entry.identity.tenant_id != logical_reference.tenant_id:
                raise result_error(
                    GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH,
                    field="catalog.reference.tenant_id",
                )
            existing = state.references.get(logical_reference.reference_id)
            if existing is not None:
                if existing != logical_reference:
                    raise result_error(
                        GraphArtifactResultErrorCode.ARTIFACT_REFERENCE_CONFLICT,
                        field="catalog.reference",
                    )
                return state, existing
            if len(state.references) >= self.max_references:
                raise result_error(
                    GraphArtifactResultErrorCode.ARTIFACT_QUOTA_EXCEEDED,
                    field="catalog.references",
                    limit=self.max_references,
                )
            references = dict(state.references)
            references[logical_reference.reference_id] = logical_reference
            return (
                _CatalogState(
                    entries=dict(state.entries),
                    claims=dict(state.claims),
                    references=references,
                ),
                logical_reference,
            )

        return self._mutate(mutate)

    def remove_reference(
        self,
        *,
        tenant_id: str,
        reference_id: str,
    ) -> bool:
        tenant = identifier(tenant_id, "catalog.tenant_id")
        normalized = reference(reference_id, "catalog.reference_id")

        def mutate(state: _CatalogState) -> tuple[_CatalogState, bool]:
            existing = state.references.get(normalized)
            if existing is None:
                return state, False
            if existing.tenant_id != tenant:
                raise result_error(
                    GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH,
                    field="catalog.reference.tenant_id",
                )
            if existing.kind not in {
                ArtifactReferenceKind.CACHE,
                ArtifactReferenceKind.EPHEMERAL,
            }:
                raise result_error(
                    GraphArtifactResultErrorCode.LIFECYCLE_AUTHORIZATION_INVALID,
                    field="catalog.reference.kind",
                )
            references = dict(state.references)
            del references[normalized]
            return (
                _CatalogState(
                    entries=dict(state.entries),
                    claims=dict(state.claims),
                    references=references,
                ),
                True,
            )

        return self._mutate(mutate)

    def snapshot(
        self,
        *,
        captured_at: datetime,
        tenant_id: str | None = None,
    ) -> ArtifactCatalogSnapshot:
        actual_time = aware_datetime(captured_at, "catalog.snapshot.captured_at")
        tenant = (
            identifier(tenant_id, "catalog.snapshot.tenant_id")
            if tenant_id is not None
            else None
        )
        return _catalog_snapshot(
            self._read_snapshot(),
            captured_at=actual_time,
            tenant_id=tenant,
        )

    def retire_reference(
        self,
        request: ArtifactReferenceRetirementRequest,
    ) -> ArtifactReferenceRetirementReceipt:
        if not isinstance(request, ArtifactReferenceRetirementRequest):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="catalog.retirement",
            )

        def mutate(
            state: _CatalogState,
        ) -> tuple[_CatalogState, ArtifactReferenceRetirementReceipt]:
            existing = state.references.get(request.reference.reference_id)
            receipt = ArtifactReferenceRetirementReceipt.create(
                request_checksum=request.request_checksum,
                reference=request.reference,
                authorization_id=request.authorization.authorization_id,
                reason=request.reason,
                retired_at=request.requested_at,
            )
            if existing is None:
                return state, receipt
            if existing != request.reference:
                raise result_error(
                    GraphArtifactResultErrorCode.ARTIFACT_REFERENCE_CONFLICT,
                    field="catalog.retirement.reference",
                )
            _validate_reference_retirement(state, request)
            references = dict(state.references)
            del references[existing.reference_id]
            return (
                _CatalogState(
                    entries=dict(state.entries),
                    claims=dict(state.claims),
                    references=references,
                ),
                receipt,
            )

        return self._mutate(mutate)

    def detach_gc_candidate(
        self,
        request: ArtifactCatalogGcDetachRequest,
    ) -> ArtifactCatalogGcDetachReceipt:
        if not isinstance(request, ArtifactCatalogGcDetachRequest):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="catalog.gc_detach",
            )

        def mutate(
            state: _CatalogState,
        ) -> tuple[_CatalogState, ArtifactCatalogGcDetachReceipt]:
            # The snapshot checksum is immutable plan evidence. Target-local
            # decision comparison is the CAS guard so earlier candidates from
            # the same multi-entry plan do not invalidate later candidates.
            entry = state.entries.get(request.decision.entry_id)
            if entry is None:
                raise result_error(
                    GraphArtifactResultErrorCode.GC_PLAN_STALE,
                    field="catalog.gc_detach.entry",
                )
            claims = _claims_for_entry(state, entry.entry_id)
            references = _references_for_entry(state, entry.entry_id)
            current_decision = _gc_decision(
                entry,
                claims,
                references,
                now=request.requested_at,
            )
            if current_decision != request.decision:
                raise result_error(
                    GraphArtifactResultErrorCode.GC_PLAN_STALE,
                    field="catalog.gc_detach.decision",
                )

            entries = dict(state.entries)
            del entries[entry.entry_id]
            claim_values = dict(state.claims)
            for claim in claims:
                del claim_values[claim.claim_id]
            reference_values = dict(state.references)
            for logical_reference in references:
                del reference_values[logical_reference.reference_id]
            receipt = ArtifactCatalogGcDetachReceipt.create(
                request_checksum=request.request_checksum,
                entry=entry,
                claims=claims,
                references=references,
                detached_at=request.requested_at,
            )
            return (
                _CatalogState(
                    entries=entries,
                    claims=claim_values,
                    references=reference_values,
                ),
                receipt,
            )

        return self._mutate(mutate)

    def plan_gc(
        self,
        *,
        now: datetime,
        tenant_id: str | None = None,
    ) -> ArtifactCatalogGcPlan:
        actual_now = aware_datetime(now, "catalog.gc.now")
        tenant = (
            identifier(tenant_id, "catalog.gc.tenant_id")
            if tenant_id is not None
            else None
        )
        state = self._read_snapshot()
        snapshot = _catalog_snapshot(
            state,
            captured_at=actual_now,
            tenant_id=tenant,
        )
        decisions = tuple(
            _gc_decision(
                entry,
                _claims_for_entry(state, entry.entry_id),
                _references_for_entry(state, entry.entry_id),
                now=actual_now,
            )
            for entry in state.entries.values()
            if tenant is None or entry.identity.tenant_id == tenant
        )
        return ArtifactCatalogGcPlan.create(
            generated_at=actual_now,
            decisions=decisions,
            catalog_snapshot_checksum=snapshot.snapshot_checksum,
        )

    def reconcile(
        self,
        *,
        now: datetime,
        tenant_id: str | None = None,
        physical_inventory: tuple[ArtifactVerificationReceipt, ...] | None = None,
    ) -> ArtifactCatalogReconciliationPlan:
        actual_now = aware_datetime(now, "catalog.reconcile.now")
        tenant = (
            identifier(tenant_id, "catalog.reconcile.tenant_id")
            if tenant_id is not None
            else None
        )
        state = self._read_snapshot(allow_drift=True)
        issues = list(state.issues)
        if physical_inventory is not None:
            issues.extend(_physical_inventory_issues(state, physical_inventory))
        if tenant is not None:
            issues = _tenant_reconciliation_issues(
                state,
                issues=issues,
                tenant_id=tenant,
            )
        return ArtifactCatalogReconciliationPlan.create(
            generated_at=actual_now,
            issues=issues,
        )

    def _mutate(
        self,
        callback: Callable[[_CatalogState], tuple[_CatalogState, T]],
    ) -> T:
        with self._thread_lock:
            self._prepare_root()
            with _exclusive_file_lock(self._lock_path):
                state = self._read_state()
                updated, result = callback(state)
                if updated != state:
                    self._write_state(updated)
                return result

    def _read_snapshot(self, *, allow_drift: bool = False) -> _CatalogState:
        with self._thread_lock:
            if not self.root.exists():
                return _CatalogState.empty()
            self._prepare_root()
            with _exclusive_file_lock(self._lock_path):
                return self._read_state(allow_drift=allow_drift)

    def _read_state(self, *, allow_drift: bool = False) -> _CatalogState:
        try:
            self._assert_safe_paths()
            if not self._state_path.exists():
                return _CatalogState.empty()
            info = os.lstat(self._state_path)
            if not stat.S_ISREG(info.st_mode) or is_link_or_reparse_point(info):
                raise _catalog_corrupt("catalog.state")
            if info.st_size > self.max_state_bytes:
                raise _catalog_corrupt("catalog.state_bytes")
            raw = self._state_path.read_bytes()
            if len(raw) != info.st_size:
                raise _catalog_corrupt("catalog.state_bytes")
            parsed = json.loads(raw.decode("utf-8"))
            state = self._decode_state(parsed)
            if state.issues and not allow_drift:
                raise _catalog_corrupt("catalog.references")
            return state
        except GraphArtifactResultError:
            raise
        except (ArtifactStoreMetadataError, OSError, UnicodeError, ValueError, TypeError) as exc:
            raise _catalog_corrupt("catalog.state") from exc

    def _decode_state(self, value: Any) -> _CatalogState:
        if not isinstance(value, Mapping) or set(value) != _STATE_FIELDS:
            raise _catalog_corrupt("catalog.state_schema")
        if value.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise _catalog_corrupt("catalog.schema_version")
        checksum_value = checksum(value.get("state_checksum"), "catalog.state_checksum")
        unsigned = {key: value[key] for key in _STATE_FIELDS if key != "state_checksum"}
        if checksum_for(unsigned) != checksum_value:
            raise _catalog_corrupt("catalog.state_checksum")

        entry_values = _object_sequence(value.get("entries"), "catalog.entries", self.max_entries)
        claim_values = _object_sequence(value.get("claims"), "catalog.claims", self.max_claims)
        reference_values = _object_sequence(
            value.get("references"),
            "catalog.references",
            self.max_references,
        )
        entries, entry_issues = _decode_models(
            entry_values,
            ArtifactCatalogEntry.from_dict,
            key=lambda item: item.entry_id,
            issue_kind=ArtifactCatalogReconciliationIssueKind.IDENTITY_CONFLICT,
        )
        claims, claim_issues = _decode_models(
            claim_values,
            ArtifactCatalogClaim.from_dict,
            key=lambda item: item.claim_id,
            issue_kind=ArtifactCatalogReconciliationIssueKind.IDENTITY_CONFLICT,
        )
        references, reference_issues = _decode_models(
            reference_values,
            ArtifactLogicalReference.from_dict,
            key=lambda item: item.reference_id,
            issue_kind=ArtifactCatalogReconciliationIssueKind.IDENTITY_CONFLICT,
        )
        issues = list(entry_issues + claim_issues + reference_issues)
        for claim in claims.values():
            if claim.entry_id not in entries:
                issues.append(
                    ArtifactCatalogReconciliationIssue.create(
                        kind=ArtifactCatalogReconciliationIssueKind.DANGLING_LOGICAL_IDENTITY,
                        subject_id=claim.claim_id,
                        entry_id=claim.entry_id,
                    )
                )
                continue
            canonical_entry = entries[claim.entry_id]
            if (
                claim.tenant_id != canonical_entry.identity.tenant_id
                or claim.record.ref != canonical_entry.record.ref
                or claim.record.byte_size != canonical_entry.record.byte_size
                or claim.record.content_checksum
                != canonical_entry.record.content_checksum
                or claim.record.media_type != canonical_entry.record.media_type
                or claim.record.producer_revision
                != canonical_entry.record.producer_revision
            ):
                issues.append(
                    ArtifactCatalogReconciliationIssue.create(
                        kind=ArtifactCatalogReconciliationIssueKind.IDENTITY_CONFLICT,
                        subject_id=claim.claim_id,
                        entry_id=claim.entry_id,
                    )
                )
        for logical_reference in references.values():
            if logical_reference.entry_id not in entries:
                issues.append(
                    ArtifactCatalogReconciliationIssue.create(
                        kind=ArtifactCatalogReconciliationIssueKind.DANGLING_REFERENCE,
                        subject_id=logical_reference.reference_id,
                        entry_id=logical_reference.entry_id,
                    )
                )
                continue
            if (
                logical_reference.tenant_id
                != entries[logical_reference.entry_id].identity.tenant_id
            ):
                issues.append(
                    ArtifactCatalogReconciliationIssue.create(
                        kind=ArtifactCatalogReconciliationIssueKind.IDENTITY_CONFLICT,
                        subject_id=logical_reference.reference_id,
                        entry_id=logical_reference.entry_id,
                    )
                )
        owned_entry_ids = {item.entry_id for item in claims.values()} | {
            item.entry_id for item in references.values()
        }
        for entry in entries.values():
            if entry.entry_id not in owned_entry_ids:
                issues.append(
                    ArtifactCatalogReconciliationIssue.create(
                        kind=ArtifactCatalogReconciliationIssueKind.ORPHAN_ENTRY,
                        subject_id=entry.entry_id,
                        entry_id=entry.entry_id,
                    )
                )
        return _CatalogState(
            entries=entries,
            claims=claims,
            references=references,
            issues=tuple(sorted(set(issues), key=lambda item: item.issue_id)),
        )

    def _write_state(self, state: _CatalogState) -> None:
        if state.issues:
            raise _catalog_corrupt("catalog.references")
        encoded = (stable_json_dumps(state.to_dict()) + "\n").encode("utf-8")
        if len(encoded) > self.max_state_bytes:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_QUOTA_EXCEEDED,
                field="catalog.state_bytes",
                actual=len(encoded),
                limit=self.max_state_bytes,
            )
        verified_atomic_write(
            self._state_path,
            encoded,
            root=self.root,
            identity="graph-artifact-catalog",
        )

    def _prepare_root(self) -> None:
        self._assert_root_not_link()
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_root_not_link()

    def _assert_safe_paths(self) -> None:
        if not self.root.exists():
            return
        self._assert_root_not_link()
        reject_link_chain(
            self._state_path,
            root=self.root,
            identity="graph-artifact-catalog",
            role="catalog state",
        )
        reject_link_chain(
            self._lock_path,
            root=self.root,
            identity="graph-artifact-catalog",
            role="catalog lock",
        )

    def _assert_root_not_link(self) -> None:
        if not self.root.exists():
            return
        info = os.lstat(self.root)
        if not stat.S_ISDIR(info.st_mode) or is_link_or_reparse_point(info):
            raise _catalog_corrupt("catalog.root")


def _validate_dedup_candidate(
    existing: ArtifactCatalogEntry,
    candidate: ArtifactCatalogEntry,
) -> None:
    if existing.identity != candidate.identity:
        raise result_error(
            GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT,
            field="catalog.identity",
        )
    if existing.record.byte_size != candidate.record.byte_size:
        raise result_error(
            GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT,
            field="catalog.byte_size",
        )


def _catalog_snapshot(
    state: _CatalogState,
    *,
    captured_at: datetime,
    tenant_id: str | None = None,
) -> ArtifactCatalogSnapshot:
    entry_ids = {
        entry.entry_id
        for entry in state.entries.values()
        if tenant_id is None or entry.identity.tenant_id == tenant_id
    }
    return ArtifactCatalogSnapshot.create(
        captured_at=captured_at,
        entries=tuple(
            entry
            for entry in state.entries.values()
            if entry.entry_id in entry_ids
        ),
        claims=tuple(
            claim
            for claim in state.claims.values()
            if claim.entry_id in entry_ids
        ),
        references=tuple(
            logical_reference
            for logical_reference in state.references.values()
            if logical_reference.entry_id in entry_ids
        ),
    )


def _claims_for_entry(
    state: _CatalogState,
    entry_id: str,
) -> tuple[ArtifactCatalogClaim, ...]:
    return tuple(
        sorted(
            (item for item in state.claims.values() if item.entry_id == entry_id),
            key=lambda item: item.claim_id,
        )
    )


def _references_for_entry(
    state: _CatalogState,
    entry_id: str,
) -> tuple[ArtifactLogicalReference, ...]:
    return tuple(
        sorted(
            (
                item
                for item in state.references.values()
                if item.entry_id == entry_id
            ),
            key=lambda item: item.reference_id,
        )
    )


def _validate_reference_retirement(
    state: _CatalogState,
    request: ArtifactReferenceRetirementRequest,
) -> None:
    logical_reference = request.reference
    authorization = request.authorization
    if authorization.policy_version != "graph-artifact-policy@1":
        raise result_error(
            GraphArtifactResultErrorCode.POLICY_VERSION_UNSUPPORTED,
            field="catalog.retirement.policy_version",
        )
    if authorization.kind is ArtifactLifecycleAuthorityKind.PUBLICATION_RETIRED:
        return
    claims = tuple(
        claim
        for claim in _claims_for_entry(state, logical_reference.entry_id)
        if claim.tenant_id == logical_reference.tenant_id
        and claim.run_id == logical_reference.owner_run_id
    )
    if not claims or any(
        claim.record.expires_at is None
        or claim.record.expires_at > request.requested_at
        for claim in claims
    ):
        raise result_error(
            GraphArtifactResultErrorCode.LIFECYCLE_AUTHORIZATION_INVALID,
            field="catalog.retirement.retention",
        )


def _gc_decision(
    entry: ArtifactCatalogEntry,
    claims: tuple[ArtifactCatalogClaim, ...],
    references: tuple[ArtifactLogicalReference, ...],
    *,
    now: datetime,
) -> ArtifactCatalogGcDecision:
    active = tuple(
        sorted(
            (item for item in references if item.is_active_at(now)),
            key=lambda item: item.reference_id,
        )
    )
    active_ids = tuple(item.reference_id for item in active)
    if entry.record.required_for_replay or any(
        item.kind is ArtifactReferenceKind.REPLAY for item in active
    ):
        action = ArtifactCatalogGcAction.KEEP
        reason = ArtifactCatalogGcReason.REPLAY_REQUIRED
    elif entry.record.required_for_publication or any(
        item.kind is ArtifactReferenceKind.PUBLICATION for item in active
    ):
        action = ArtifactCatalogGcAction.KEEP
        reason = ArtifactCatalogGcReason.PUBLICATION_REQUIRED
    elif active:
        action = ArtifactCatalogGcAction.KEEP
        reason = ArtifactCatalogGcReason.REFERENCE_PROTECTED
    elif entry.record.expires_at is None:
        action = ArtifactCatalogGcAction.KEEP
        reason = ArtifactCatalogGcReason.RETENTION_INDEFINITE
    elif entry.record.expires_at > now:
        action = ArtifactCatalogGcAction.KEEP
        reason = ArtifactCatalogGcReason.RETENTION_ACTIVE
    else:
        action = ArtifactCatalogGcAction.DELETE_CANDIDATE
        reason = ArtifactCatalogGcReason.EXPIRED_UNREFERENCED
    return ArtifactCatalogGcDecision(
        entry_id=entry.entry_id,
        tenant_id=entry.identity.tenant_id,
        ref=entry.record.ref,
        action=action,
        reason=reason,
        active_reference_ids=active_ids,
        byte_size=entry.record.byte_size,
        claim_ids=tuple(sorted(item.claim_id for item in claims)),
        reference_ids=tuple(sorted(item.reference_id for item in references)),
    )


def _physical_inventory_issues(
    state: _CatalogState,
    inventory: tuple[ArtifactVerificationReceipt, ...],
) -> tuple[ArtifactCatalogReconciliationIssue, ...]:
    if not isinstance(inventory, tuple) or not all(
        isinstance(item, ArtifactVerificationReceipt) for item in inventory
    ):
        raise result_error(
            GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
            field="catalog.physical_inventory",
        )
    by_ref: dict[str, ArtifactVerificationReceipt] = {}
    issues: list[ArtifactCatalogReconciliationIssue] = []
    for receipt in inventory:
        existing = by_ref.get(receipt.ref)
        if existing is not None and existing != receipt:
            issues.append(
                ArtifactCatalogReconciliationIssue.create(
                    kind=ArtifactCatalogReconciliationIssueKind.PHYSICAL_IDENTITY_MISMATCH,
                    subject_id=receipt.ref,
                )
            )
            continue
        by_ref[receipt.ref] = receipt

    entries_by_ref = {entry.record.ref: entry for entry in state.entries.values()}
    for entry in state.entries.values():
        receipt = by_ref.get(entry.record.ref)
        if receipt is None:
            issues.append(
                ArtifactCatalogReconciliationIssue.create(
                    kind=ArtifactCatalogReconciliationIssueKind.MISSING_PHYSICAL_OBJECT,
                    subject_id=entry.record.ref,
                    entry_id=entry.entry_id,
                )
            )
            continue
        try:
            receipt.verify(entry.record)
        except GraphArtifactResultError:
            issues.append(
                ArtifactCatalogReconciliationIssue.create(
                    kind=ArtifactCatalogReconciliationIssueKind.PHYSICAL_IDENTITY_MISMATCH,
                    subject_id=entry.record.ref,
                    entry_id=entry.entry_id,
                )
            )
    for receipt in inventory:
        if receipt.ref not in entries_by_ref:
            issues.append(
                ArtifactCatalogReconciliationIssue.create(
                    kind=ArtifactCatalogReconciliationIssueKind.UNREGISTERED_PHYSICAL_OBJECT,
                    subject_id=receipt.ref,
                )
            )
    return tuple(sorted(set(issues), key=lambda item: item.issue_id))


def _tenant_reconciliation_issues(
    state: _CatalogState,
    *,
    issues: list[ArtifactCatalogReconciliationIssue],
    tenant_id: str,
) -> list[ArtifactCatalogReconciliationIssue]:
    entry_ids = {
        entry.entry_id
        for entry in state.entries.values()
        if entry.identity.tenant_id == tenant_id
    }
    subject_ids = set(entry_ids)
    subject_ids.update(
        claim.claim_id
        for claim in state.claims.values()
        if claim.tenant_id == tenant_id
    )
    subject_ids.update(
        reference.reference_id
        for reference in state.references.values()
        if reference.tenant_id == tenant_id
    )
    return [
        issue
        for issue in issues
        if issue.subject_id in subject_ids
        or (issue.entry_id is not None and issue.entry_id in entry_ids)
    ]


def _decode_models(
    values: Sequence[Mapping[str, Any]],
    decoder: Callable[[Mapping[str, Any]], T],
    *,
    key: Callable[[T], str],
    issue_kind: ArtifactCatalogReconciliationIssueKind,
) -> tuple[dict[str, T], tuple[ArtifactCatalogReconciliationIssue, ...]]:
    result: dict[str, T] = {}
    issues: list[ArtifactCatalogReconciliationIssue] = []
    for raw in values:
        try:
            model = decoder(raw)
        except GraphArtifactResultError as exc:
            raise _catalog_corrupt("catalog.record") from exc
        model_id = key(model)
        existing = result.get(model_id)
        if existing is not None:
            issues.append(
                ArtifactCatalogReconciliationIssue.create(
                    kind=issue_kind,
                    subject_id=model_id,
                    entry_id=(
                        model_id
                        if isinstance(model, ArtifactCatalogEntry)
                        else getattr(model, "entry_id", None)
                    ),
                )
            )
            if existing != model:
                continue
        result[model_id] = model
    return result, tuple(issues)


def _object_sequence(value: Any, field: str, limit: int) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _catalog_corrupt(field)
    if len(value) > limit:
        raise _catalog_corrupt(field)
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise _catalog_corrupt(field)
        result.append(item)
    return tuple(result)


def _positive_bound(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _catalog_corrupt(field: str) -> GraphArtifactResultError:
    return result_error(
        GraphArtifactResultErrorCode.ARTIFACT_CATALOG_CORRUPT,
        field=field,
    )


def _path_lock(path: Path) -> threading.RLock:
    key = path.resolve(strict=False)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or is_link_or_reparse_point(existing)
    ):
        raise _catalog_corrupt("catalog.lock")

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _catalog_corrupt("catalog.lock") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _catalog_corrupt("catalog.lock")
        if os.name == "nt":
            import msvcrt

            if info.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise _catalog_corrupt("catalog.lock") from exc
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(descriptor)


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "DEFAULT_MAX_CATALOG_CLAIMS",
    "DEFAULT_MAX_CATALOG_ENTRIES",
    "DEFAULT_MAX_CATALOG_REFERENCES",
    "DEFAULT_MAX_CATALOG_STATE_BYTES",
    "LocalJsonArtifactCatalog",
]
