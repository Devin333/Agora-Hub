from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

from framework.events.canonical import checksum_for
from framework.harness.artifacts.governance import (
    GraphArtifactUsageFact,
    GraphArtifactUsageKind,
    GraphArtifactUsageOutcome,
    GraphArtifactUsagePort,
    GraphArtifactUsageReason,
)
from framework.harness.artifacts.catalog import (
    ArtifactCatalogRegistrationRequest,
    ArtifactCatalogRegistrationResult,
    ArtifactVerificationReceipt,
)
from framework.harness.artifacts.ports import (
    ArtifactCatalogPort,
    ArtifactPort,
    ArtifactRef,
    ArtifactWriteRequest,
)
from framework.harness.runtime.result_canonical import (
    aware_datetime,
    checksum,
    datetime_from_json,
    datetime_to_json,
    exact_keys,
    exact_reference,
    identifier,
    media_type,
    non_negative_int,
    reference,
    serialize_candidate,
    sha256_checksum,
)
from framework.harness.runtime.result_errors import (
    GraphArtifactResultError,
    GraphArtifactResultErrorCode,
    result_error,
)
from framework.harness.runtime.result_models import (
    ArtifactClass,
    ArtifactRecord,
    CacheRef,
    NodeResultBinding,
    NodeResultEnvelope,
    PersistenceDecision,
    PersistenceMode,
    PersistenceReason,
    ResultMetrics,
    RetentionClass,
)
from framework.harness.runtime.result_policy import (
    NodeResultRequest,
    PersistenceBudgetSnapshot,
    PersistenceEvaluation,
    PersistencePolicy,
)
from framework.harness.graph.canonical import thaw_json
from framework.shared.json import stable_json_dumps
from framework.shared.time import utc_now

if TYPE_CHECKING:
    from framework.harness.artifacts.governance import GraphArtifactQuotaSnapshot


RESULT_PAYLOAD_SCHEMA = "newsroom.graph-result-payload@1"


class ResultMaterializationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    OMITTED = "omitted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResultQuotaReservation:
    reservation_id: str
    tenant_id: str
    run_id: str
    graph_id: str
    node_id: str
    artifact_class: ArtifactClass
    retention_class: RetentionClass
    policy_version: str
    reservation_key: str
    generation: int
    reserved_bytes: int
    object_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "reservation_id", reference(self.reservation_id, "reservation.id"))
        object.__setattr__(self, "tenant_id", identifier(self.tenant_id, "reservation.tenant_id"))
        object.__setattr__(self, "run_id", identifier(self.run_id, "reservation.run_id"))
        object.__setattr__(self, "graph_id", identifier(self.graph_id, "reservation.graph_id"))
        object.__setattr__(self, "node_id", identifier(self.node_id, "reservation.node_id"))
        try:
            artifact_class = ArtifactClass(self.artifact_class)
            retention_class = RetentionClass(self.retention_class)
        except (TypeError, ValueError) as exc:
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="reservation.class",
            ) from exc
        object.__setattr__(self, "artifact_class", artifact_class)
        object.__setattr__(self, "retention_class", retention_class)
        object.__setattr__(
            self,
            "policy_version",
            exact_reference(self.policy_version, "reservation.policy_version"),
        )
        object.__setattr__(self, "reservation_key", reference(self.reservation_key, "reservation.key"))
        generation = non_negative_int(self.generation, "reservation.generation")
        if generation < 1:
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="reservation.generation",
            )
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "reserved_bytes", non_negative_int(self.reserved_bytes, "reservation.bytes"))
        object.__setattr__(self, "object_count", non_negative_int(self.object_count, "reservation.object_count"))
        if self.object_count < 1:
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="reservation.object_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "node_id": self.node_id,
            "artifact_class": self.artifact_class.value,
            "retention_class": self.retention_class.value,
            "policy_version": self.policy_version,
            "reservation_key": self.reservation_key,
            "generation": self.generation,
            "reserved_bytes": self.reserved_bytes,
            "object_count": self.object_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        return cls(
            **exact_keys(
                value,
                required=frozenset(
                    {
                        "reservation_id",
                        "tenant_id",
                        "run_id",
                        "graph_id",
                        "node_id",
                        "artifact_class",
                        "retention_class",
                        "policy_version",
                        "reservation_key",
                        "generation",
                        "reserved_bytes",
                        "object_count",
                    }
                ),
                model=cls.__name__,
            )
        )


@dataclass(frozen=True, slots=True)
class ResultQuotaReconciliationEvidence:
    reservation_id: str
    attempt_committed: bool
    catalog_claim_committed: bool
    cache_entry_committed: bool
    physical_operation_committed: bool
    evidence_refs: tuple[str, ...]
    observed_at: datetime
    evidence_checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reservation_id",
            reference(self.reservation_id, "quota_reconciliation.reservation_id"),
        )
        for name in (
            "attempt_committed",
            "catalog_claim_committed",
            "cache_entry_committed",
            "physical_operation_committed",
        ):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise result_error(
                    GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                    field=f"quota_reconciliation.{name}",
                )
        refs = tuple(reference(item, "quota_reconciliation.evidence_refs") for item in self.evidence_refs)
        if refs != tuple(sorted(set(refs))) or not refs:
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="quota_reconciliation.evidence_refs",
            )
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(
            self,
            "observed_at",
            aware_datetime(self.observed_at, "quota_reconciliation.observed_at"),
        )
        expected = checksum_for(self.checksum_projection())
        if checksum(self.evidence_checksum, "quota_reconciliation.evidence_checksum") != expected:
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT,
                field="quota_reconciliation.evidence_checksum",
            )
        object.__setattr__(self, "evidence_checksum", expected)

    @property
    def proves_absence(self) -> bool:
        return not any(
            (
                self.attempt_committed,
                self.catalog_claim_committed,
                self.cache_entry_committed,
                self.physical_operation_committed,
            )
        )

    @classmethod
    def create(cls, **values: Any) -> Self:
        projection = _quota_reconciliation_projection(values)
        return cls(**values, evidence_checksum=checksum_for(projection))

    def checksum_projection(self) -> dict[str, Any]:
        return _quota_reconciliation_projection(
            {
                "reservation_id": self.reservation_id,
                "attempt_committed": self.attempt_committed,
                "catalog_claim_committed": self.catalog_claim_committed,
                "cache_entry_committed": self.cache_entry_committed,
                "physical_operation_committed": self.physical_operation_committed,
                "evidence_refs": self.evidence_refs,
                "observed_at": self.observed_at,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "evidence_checksum": self.evidence_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "reservation_id",
                    "attempt_committed",
                    "catalog_claim_committed",
                    "cache_entry_committed",
                    "physical_operation_committed",
                    "evidence_refs",
                    "observed_at",
                    "evidence_checksum",
                }
            ),
            model=cls.__name__,
        )
        payload["evidence_refs"] = tuple(payload["evidence_refs"])
        payload["observed_at"] = datetime_from_json(
            payload["observed_at"],
            "quota_reconciliation.observed_at",
        )
        return cls(**payload)


@runtime_checkable
class ResultQuotaPort(Protocol):
    """Reserve and settle durable result quota outside the framework domain."""

    def reserve(
        self,
        *,
        tenant_id: str,
        run_id: str,
        graph_id: str,
        node_id: str,
        artifact_class: ArtifactClass,
        retention_class: RetentionClass,
        policy_version: str,
        reservation_key: str,
        requested_bytes: int,
        object_count: int,
    ) -> ResultQuotaReservation | None:
        ...

    def settle(
        self,
        reservation: ResultQuotaReservation,
        *,
        actual_bytes: int,
        object_count: int,
        outcome: ResultMaterializationOutcome,
        usage_fact: GraphArtifactUsageFact | None = None,
    ) -> None:
        ...

    def reconcile_pending(
        self,
        evidence: ResultQuotaReconciliationEvidence,
    ) -> ResultQuotaReservation:
        ...

    def quota_snapshots(
        self,
        *,
        tenant_id: str,
        captured_at: datetime,
    ) -> tuple["GraphArtifactQuotaSnapshot", ...]:
        ...


@dataclass(frozen=True, slots=True)
class ResultCacheWriteRequest:
    cache_key: str
    tenant_id: str
    payload: Mapping[str, Any]
    media_type: str
    content_checksum: str
    byte_size: int
    dependency_digest: str
    policy_version: str
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "cache_key", reference(self.cache_key, "cache_write.key"))
        object.__setattr__(self, "tenant_id", identifier(self.tenant_id, "cache_write.tenant_id"))
        if not isinstance(self.payload, Mapping):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="cache_write.payload")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "media_type", media_type(self.media_type, "cache_write.media_type"))
        object.__setattr__(self, "content_checksum", checksum(self.content_checksum, "cache_write.content_checksum"))
        object.__setattr__(self, "byte_size", non_negative_int(self.byte_size, "cache_write.byte_size"))
        object.__setattr__(self, "dependency_digest", checksum(self.dependency_digest, "cache_write.dependency_digest"))
        object.__setattr__(self, "policy_version", exact_reference(self.policy_version, "cache_write.policy_version"))
        object.__setattr__(self, "expires_at", aware_datetime(self.expires_at, "cache_write.expires_at"))


@runtime_checkable
class ResultCachePort(Protocol):
    def write(self, request: ResultCacheWriteRequest) -> str:
        ...

    def read(self, ref: str) -> Mapping[str, Any]:
        ...


@runtime_checkable
class ResultAttemptLedgerPort(Protocol):
    """Durable first-writer-wins envelope ledger for one node attempt."""

    def get(self, binding: NodeResultBinding) -> NodeResultEnvelope | None:
        ...

    def put(self, envelope: NodeResultEnvelope) -> NodeResultEnvelope:
        ...


@dataclass(frozen=True, slots=True)
class ResultMaterializationObservation:
    binding: NodeResultBinding
    outcome: ResultMaterializationOutcome
    mode: PersistenceMode
    candidate_bytes: int
    reservation_id: str | None = None
    reason: PersistenceReason | None = None
    error_code: GraphArtifactResultErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, NodeResultBinding):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="observation.binding")
        object.__setattr__(self, "outcome", ResultMaterializationOutcome(self.outcome))
        object.__setattr__(self, "mode", PersistenceMode(self.mode))
        object.__setattr__(self, "candidate_bytes", non_negative_int(self.candidate_bytes, "observation.candidate_bytes"))
        if self.reservation_id is not None:
            object.__setattr__(self, "reservation_id", reference(self.reservation_id, "observation.reservation_id"))
        if self.reason is not None:
            object.__setattr__(self, "reason", PersistenceReason(self.reason))
        if self.error_code is not None:
            object.__setattr__(self, "error_code", GraphArtifactResultErrorCode(self.error_code))

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "outcome": self.outcome.value,
            "mode": self.mode.value,
            "candidate_bytes": self.candidate_bytes,
            "reservation_id": self.reservation_id,
            "reason": self.reason.value if self.reason is not None else None,
            "error_code": self.error_code.value if self.error_code is not None else None,
        }


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    envelope: NodeResultEnvelope
    observation: ResultMaterializationObservation

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, NodeResultEnvelope):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="materialization.envelope")
        if not isinstance(self.observation, ResultMaterializationObservation):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="materialization.observation")
        if self.observation.binding != self.envelope.binding:
            raise result_error(GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT, field="materialization.binding")


class ResultMaterializer:
    """The Harness-owned, bounded transition from a worker candidate to a result envelope."""

    def __init__(
        self,
        *,
        policy: PersistencePolicy,
        artifact_port: ArtifactPort,
        catalog: ArtifactCatalogPort,
        quota: ResultQuotaPort,
        usage: GraphArtifactUsagePort,
        cache: ResultCachePort,
        attempts: ResultAttemptLedgerPort,
        clock: Callable[[], datetime] = utc_now,
        observation_sink: Callable[[ResultMaterializationObservation], None] | None = None,
    ) -> None:
        if not isinstance(policy, PersistencePolicy):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="materializer.policy")
        for value, field_name in (
            (artifact_port, "artifact_port"),
            (catalog, "catalog"),
            (quota, "quota"),
            (usage, "usage"),
            (cache, "cache"),
            (attempts, "attempts"),
        ):
            if value is None:
                raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field=f"materializer.{field_name}")
        if not callable(getattr(artifact_port, "bind_run", None)):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="materializer.artifact_port.bind_run")
        if not callable(clock):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="materializer.clock")
        if observation_sink is not None and not callable(observation_sink):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="materializer.observation_sink")
        self._policy = policy
        self._artifact_port = artifact_port
        self._catalog = catalog
        self._quota = quota
        self._usage = usage
        self._cache = cache
        self._attempts = attempts
        self._clock = clock
        self._observation_sink = observation_sink

    @property
    def artifact_catalog(self) -> ArtifactCatalogPort:
        """The metadata owner used when committing materialized artifacts."""
        return self._catalog

    def materialize(
        self,
        request: NodeResultRequest,
        *,
        budget: PersistenceBudgetSnapshot | None = None,
    ) -> MaterializationResult:
        if not isinstance(request, NodeResultRequest):
            raise result_error(GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID, field="request")
        existing = self._get_existing(request)
        if existing is not None:
            self._assert_existing_compatible(
                existing,
                request,
                policy_version=self._policy.config.policy_version,
            )
            self._record_usage(
                self._usage_fact(
                    request,
                    policy_version=existing.persistence_decision.policy_version,
                    kind=GraphArtifactUsageKind.MATERIALIZATION,
                    outcome=GraphArtifactUsageOutcome.SUCCEEDED,
                    stage="recovered",
                    reason_code=GraphArtifactUsageReason.RECOVERED.value,
                )
            )
            observation = self._observation(
                request,
                outcome=ResultMaterializationOutcome.SUCCEEDED,
                mode=existing.persistence_decision.mode,
                candidate_bytes=request.candidate_bytes,
                reason=existing.persistence_decision.reason,
            )
            self._emit(observation)
            return MaterializationResult(envelope=existing, observation=observation)

        evaluation: PersistenceEvaluation | None = None
        reservation: ResultQuotaReservation | None = None
        settlement_attempted = False
        try:
            evaluation = self._policy.evaluate(request, budget=budget)
            decision = evaluation.decision
            if decision.mode in {PersistenceMode.ARTIFACT, PersistenceMode.CACHE}:
                reservation = self._reserve(request, evaluation)
                if reservation is None:
                    if decision.required:
                        raise result_error(
                            GraphArtifactResultErrorCode.ARTIFACT_QUOTA_EXCEEDED,
                            mode=decision.mode,
                            required=True,
                        )
                    evaluation = self._omitted_evaluation(request)
                    envelope = self._build_envelope(request, evaluation)
                    self._record_usage(
                        self._materialization_usage_fact(
                            request,
                            evaluation,
                            ResultMaterializationOutcome.OMITTED,
                        )
                    )
                    observation = self._observation(
                        request,
                        outcome=ResultMaterializationOutcome.OMITTED,
                        mode=PersistenceMode.OMITTED,
                        candidate_bytes=request.candidate_bytes,
                        reason=PersistenceReason.QUOTA_EXCEEDED,
                    )
                    stored = self._put_attempt(envelope)
                    if stored != envelope:
                        envelope = stored
                    self._emit(observation)
                    return MaterializationResult(envelope=envelope, observation=observation)
            if decision.mode is PersistenceMode.INLINE:
                envelope = self._build_envelope(request, evaluation)
            elif decision.mode is PersistenceMode.ARTIFACT:
                record = self._materialize_artifact(request)
                envelope = self._build_envelope(request, evaluation, records=(record,))
            elif decision.mode is PersistenceMode.CACHE:
                cache_ref = self._materialize_cache(request, evaluation)
                envelope = self._build_envelope(request, evaluation, caches=(cache_ref,))
            else:
                envelope = self._build_envelope(request, evaluation)
            usage_fact = self._materialization_usage_fact(
                request,
                evaluation,
                ResultMaterializationOutcome.SUCCEEDED,
            )
            if reservation is not None:
                settlement_attempted = True
                self._settle(
                    reservation,
                    request,
                    ResultMaterializationOutcome.SUCCEEDED,
                    usage_fact=usage_fact,
                )
            else:
                self._record_usage(usage_fact)
            self._record_success_boundary_usage(request, evaluation)
            stored = self._put_attempt(envelope)
            if stored != envelope:
                self._assert_existing_compatible(
                    stored,
                    request,
                    policy_version=evaluation.decision.policy_version,
                )
                envelope = stored
            observation = self._observation(
                request,
                outcome=ResultMaterializationOutcome.SUCCEEDED,
                mode=envelope.persistence_decision.mode,
                candidate_bytes=request.candidate_bytes,
                reservation_id=reservation.reservation_id if reservation is not None else None,
                reason=envelope.persistence_decision.reason,
            )
            self._emit(observation)
            return MaterializationResult(envelope=envelope, observation=observation)
        except GraphArtifactResultError as exc:
            accounting_error = self._account_failure(
                request,
                evaluation=evaluation,
                reservation=reservation,
                settlement_attempted=settlement_attempted,
                error_code=exc.error_code,
            )
            reported_error = accounting_error or exc
            self._emit(
                self._observation(
                    request,
                    outcome=ResultMaterializationOutcome.FAILED,
                    mode=evaluation.decision.mode if evaluation is not None else PersistenceMode.OMITTED,
                    candidate_bytes=request.candidate_bytes,
                    reservation_id=reservation.reservation_id if reservation is not None else None,
                    reason=evaluation.decision.reason if evaluation is not None else None,
                    error_code=reported_error.error_code,
                )
            )
            if accounting_error is not None:
                raise accounting_error from exc
            raise
        except Exception as exc:
            error = result_error(
                GraphArtifactResultErrorCode.ARTIFACT_WRITE_FAILED,
                mode=evaluation.decision.mode if evaluation is not None else PersistenceMode.OMITTED,
            )
            accounting_error = self._account_failure(
                request,
                evaluation=evaluation,
                reservation=reservation,
                settlement_attempted=settlement_attempted,
                error_code=error.error_code,
            )
            reported_error = accounting_error or error
            self._emit(
                self._observation(
                    request,
                    outcome=ResultMaterializationOutcome.FAILED,
                    mode=evaluation.decision.mode if evaluation is not None else PersistenceMode.OMITTED,
                    candidate_bytes=request.candidate_bytes,
                    reservation_id=reservation.reservation_id if reservation is not None else None,
                    reason=evaluation.decision.reason if evaluation is not None else None,
                    error_code=reported_error.error_code,
                )
            )
            if accounting_error is not None:
                raise accounting_error from exc
            raise error from exc

    def recover(self, binding: NodeResultBinding) -> NodeResultEnvelope | None:
        """Read a previously committed attempt envelope without re-running a producer."""

        if not isinstance(binding, NodeResultBinding):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="binding",
            )
        try:
            existing = self._attempts.get(binding)
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_LEDGER_FAILED,
                field="attempt.get",
            ) from exc
        if existing is not None and not isinstance(existing, NodeResultEnvelope):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_LEDGER_FAILED,
                field="attempt.envelope",
            )
        return existing

    def require_existing(self, request: NodeResultRequest) -> NodeResultEnvelope:
        """Require a readable, request-compatible envelope without creating bytes."""

        if not isinstance(request, NodeResultRequest):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
                field="request",
            )
        existing = self.recover(request.binding)
        if existing is None:
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_LEDGER_FAILED,
                field="attempt.missing",
            )
        policy_version = self._policy.config.ensure_readable_policy_version(
            existing.persistence_decision.policy_version
        )
        self._assert_existing_compatible(
            existing,
            request,
            policy_version=policy_version,
        )
        decision = existing.persistence_decision
        if (
            existing.summary != request.summary
            or existing.provenance != request.provenance
            or existing.created_at != request.created_at
            or decision.artifact_class is not request.artifact_class
            or decision.retention_class is not request.retention_class
            or decision.context_policy is not request.context_policy
            or decision.required != request.required
        ):
            raise result_error(
                GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT,
                field="attempt",
            )
        return existing

    def _get_existing(self, request: NodeResultRequest) -> NodeResultEnvelope | None:
        try:
            existing = self._attempts.get(request.binding)
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(GraphArtifactResultErrorCode.RESULT_LEDGER_FAILED, field="attempt.get") from exc
        if existing is not None and not isinstance(existing, NodeResultEnvelope):
            raise result_error(GraphArtifactResultErrorCode.RESULT_LEDGER_FAILED, field="attempt.envelope")
        return existing

    def _put_attempt(self, envelope: NodeResultEnvelope) -> NodeResultEnvelope:
        try:
            stored = self._attempts.put(envelope)
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(GraphArtifactResultErrorCode.RESULT_LEDGER_FAILED, field="attempt.put") from exc
        if not isinstance(stored, NodeResultEnvelope):
            raise result_error(GraphArtifactResultErrorCode.RESULT_LEDGER_FAILED, field="attempt.envelope")
        return stored

    def _assert_existing_compatible(
        self,
        existing: NodeResultEnvelope,
        request: NodeResultRequest,
        *,
        policy_version: str,
    ) -> None:
        if (
            existing.binding != request.binding
            or existing.candidate_checksum != request.candidate_checksum
            or existing.output_schema_ref != request.output_schema_ref
            or existing.output_schema_digest != request.output_schema_digest
            or existing.status != request.status
            or existing.persistence_decision.policy_version != policy_version
        ):
            raise result_error(GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT, field="attempt")

    def _reserve(
        self,
        request: NodeResultRequest,
        evaluation: PersistenceEvaluation,
    ) -> ResultQuotaReservation | None:
        try:
            reservation = self._quota.reserve(
                tenant_id=request.binding.tenant_id,
                run_id=request.binding.run_id,
                graph_id=request.binding.graph_id,
                node_id=request.binding.node_id,
                artifact_class=evaluation.decision.artifact_class,
                retention_class=evaluation.decision.retention_class,
                policy_version=evaluation.decision.policy_version,
                reservation_key=self._quota_key(request, evaluation),
                requested_bytes=evaluation.decision.reserved_bytes,
                object_count=1,
            )
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(GraphArtifactResultErrorCode.ARTIFACT_QUOTA_RESERVATION_FAILED, field="quota.reserve") from exc
        if reservation is not None and not isinstance(reservation, ResultQuotaReservation):
            raise result_error(GraphArtifactResultErrorCode.ARTIFACT_QUOTA_RESERVATION_FAILED, field="quota.reservation")
        if reservation is not None and (
            reservation.tenant_id != request.binding.tenant_id
            or reservation.run_id != request.binding.run_id
            or reservation.graph_id != request.binding.graph_id
            or reservation.node_id != request.binding.node_id
            or reservation.artifact_class is not evaluation.decision.artifact_class
            or reservation.retention_class is not evaluation.decision.retention_class
            or reservation.policy_version != evaluation.decision.policy_version
            or reservation.reserved_bytes != evaluation.decision.reserved_bytes
        ):
            raise result_error(GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH, field="quota.reservation")
        return reservation

    def _settle(
        self,
        reservation: ResultQuotaReservation | None,
        request: NodeResultRequest,
        outcome: ResultMaterializationOutcome,
        *,
        usage_fact: GraphArtifactUsageFact,
    ) -> None:
        if reservation is None:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_QUOTA_SETTLEMENT_FAILED,
                field="quota.reservation",
            )
        try:
            self._quota.settle(
                reservation,
                actual_bytes=request.candidate_bytes if outcome is ResultMaterializationOutcome.SUCCEEDED else 0,
                object_count=1 if outcome is ResultMaterializationOutcome.SUCCEEDED else 0,
                outcome=outcome,
                usage_fact=usage_fact,
            )
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_QUOTA_SETTLEMENT_FAILED,
                field="quota.settle",
            ) from exc

    def _record_usage(self, fact: GraphArtifactUsageFact) -> GraphArtifactUsageFact:
        try:
            stored = self._usage.record_usage(fact)
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(
                GraphArtifactResultErrorCode.GOVERNANCE_LEDGER_FAILED,
                field="usage.record",
            ) from exc
        if not isinstance(stored, GraphArtifactUsageFact) or stored != fact:
            raise result_error(
                GraphArtifactResultErrorCode.GOVERNANCE_LEDGER_FAILED,
                field="usage.fact",
            )
        return stored

    def _materialization_usage_fact(
        self,
        request: NodeResultRequest,
        evaluation: PersistenceEvaluation,
        outcome: ResultMaterializationOutcome,
        *,
        error_code: GraphArtifactResultErrorCode | None = None,
    ) -> GraphArtifactUsageFact:
        mode = evaluation.decision.mode
        if error_code is not None:
            usage_outcome = GraphArtifactUsageOutcome.FAILED
            reason_code = error_code.value
            physical_bytes = 0
            object_count = 0
        elif outcome is ResultMaterializationOutcome.OMITTED:
            usage_outcome = GraphArtifactUsageOutcome.OMITTED
            reason_code = GraphArtifactUsageReason.RESULT_OMITTED.value
            physical_bytes = 0
            object_count = 0
        else:
            usage_outcome = GraphArtifactUsageOutcome.SUCCEEDED
            reason_code = (
                GraphArtifactUsageReason.INLINE_RESULT.value
                if mode is PersistenceMode.INLINE
                else (
                    GraphArtifactUsageReason.ARTIFACT_RESULT.value
                    if mode is PersistenceMode.ARTIFACT
                    else GraphArtifactUsageReason.CACHE_RESULT.value
                )
            )
            physical_bytes = (
                request.candidate_bytes
                if mode in {PersistenceMode.ARTIFACT, PersistenceMode.CACHE}
                else 0
            )
            object_count = 1
        return self._usage_fact(
            request,
            policy_version=evaluation.decision.policy_version,
            kind=GraphArtifactUsageKind.MATERIALIZATION,
            outcome=usage_outcome,
            stage=f"materialization:{mode.value}:{usage_outcome.value}",
            reason_code=reason_code,
            logical_bytes=request.candidate_bytes,
            physical_bytes=physical_bytes,
            object_count=object_count,
        )

    def _record_success_boundary_usage(
        self,
        request: NodeResultRequest,
        evaluation: PersistenceEvaluation,
    ) -> None:
        mode = evaluation.decision.mode
        if mode is PersistenceMode.ARTIFACT:
            self._record_usage(
                self._usage_fact(
                    request,
                    policy_version=evaluation.decision.policy_version,
                    kind=GraphArtifactUsageKind.ARTIFACT_READBACK,
                    outcome=GraphArtifactUsageOutcome.SUCCEEDED,
                    stage="artifact:readback",
                    reason_code=GraphArtifactUsageReason.MATERIALIZED_RESULT.value,
                    loaded_bytes=request.candidate_bytes,
                    loaded_tokens=request.candidate_tokens,
                )
            )
        elif mode is PersistenceMode.CACHE:
            for kind, stage, reason_code, physical_bytes, loaded_bytes in (
                (
                    GraphArtifactUsageKind.CACHE_WRITE,
                    "cache:write",
                    GraphArtifactUsageReason.CACHE_WRITE.value,
                    request.candidate_bytes,
                    0,
                ),
                (
                    GraphArtifactUsageKind.CACHE_READBACK,
                    "cache:readback",
                    GraphArtifactUsageReason.CACHE_READBACK.value,
                    0,
                    request.candidate_bytes,
                ),
            ):
                self._record_usage(
                    self._usage_fact(
                        request,
                        policy_version=evaluation.decision.policy_version,
                        kind=kind,
                        outcome=GraphArtifactUsageOutcome.SUCCEEDED,
                        stage=stage,
                        reason_code=reason_code,
                        physical_bytes=physical_bytes,
                        loaded_bytes=loaded_bytes,
                        loaded_tokens=(
                            request.candidate_tokens if loaded_bytes else 0
                        ),
                        object_count=1 if physical_bytes else 0,
                    )
                )

    def _account_failure(
        self,
        request: NodeResultRequest,
        *,
        evaluation: PersistenceEvaluation | None,
        reservation: ResultQuotaReservation | None,
        settlement_attempted: bool,
        error_code: GraphArtifactResultErrorCode,
    ) -> GraphArtifactResultError | None:
        if error_code in {
            GraphArtifactResultErrorCode.GOVERNANCE_LEDGER_FAILED,
            GraphArtifactResultErrorCode.ARTIFACT_QUOTA_SETTLEMENT_FAILED,
        }:
            return None
        actual_evaluation = evaluation or self._omitted_evaluation(request)
        primary = self._materialization_usage_fact(
            request,
            actual_evaluation,
            ResultMaterializationOutcome.FAILED,
            error_code=error_code,
        )
        try:
            if reservation is not None and not settlement_attempted:
                self._settle(
                    reservation,
                    request,
                    ResultMaterializationOutcome.FAILED,
                    usage_fact=primary,
                )
            else:
                self._record_usage(primary)
            boundary_kind = _failure_usage_kind(error_code)
            if boundary_kind is not None:
                self._record_usage(
                    self._usage_fact(
                        request,
                        policy_version=actual_evaluation.decision.policy_version,
                        kind=boundary_kind,
                        outcome=GraphArtifactUsageOutcome.FAILED,
                        stage=f"failure:{error_code.value}",
                        reason_code=error_code.value,
                    )
                )
        except GraphArtifactResultError as exc:
            return exc
        return None

    def _usage_fact(
        self,
        request: NodeResultRequest,
        *,
        policy_version: str,
        kind: GraphArtifactUsageKind,
        outcome: GraphArtifactUsageOutcome,
        stage: str,
        reason_code: str,
        logical_bytes: int = 0,
        physical_bytes: int = 0,
        loaded_bytes: int = 0,
        loaded_tokens: int = 0,
        object_count: int = 0,
    ) -> GraphArtifactUsageFact:
        return GraphArtifactUsageFact.create(
            kind=kind,
            outcome=outcome,
            tenant_id=request.binding.tenant_id,
            run_id=request.binding.run_id,
            graph_id=request.binding.graph_id,
            node_id=request.binding.node_id,
            artifact_class=request.artifact_class,
            retention_class=request.retention_class,
            policy_version=policy_version,
            operation_id=_usage_operation_id(
                request,
                policy_version=policy_version,
                stage=stage,
            ),
            logical_bytes=logical_bytes,
            physical_bytes=physical_bytes,
            loaded_bytes=loaded_bytes,
            loaded_tokens=loaded_tokens,
            object_count=object_count,
            reason_code=reason_code,
            occurred_at=request.created_at,
        )

    def _materialize_artifact(self, request: NodeResultRequest) -> ArtifactRecord:
        artifact_id = self._artifact_id(request)
        artifact_type = self._artifact_type(request)
        payload = _candidate_payload(request)
        write_request = ArtifactWriteRequest(
            artifact_type=artifact_type,
            payload=payload,
            media_type="application/json",
            metadata={
                "tenant_id": request.binding.tenant_id,
                "run_id": request.binding.run_id,
                "graph_id": request.binding.graph_id,
                "node_id": request.binding.node_id,
                "attempt_id": request.binding.attempt_id,
                "candidate_checksum": request.candidate_checksum,
                "graph_result_ref_only": True,
                "identity_checksum": _artifact_identity_checksum(artifact_type),
                "required_for_replay": request.required_for_replay,
                "required_for_publication": request.required_for_publication,
            },
        )
        try:
            with self._bound_run(request.binding.run_id):
                ref = self._artifact_port.write_artifact(write_request)
                if not isinstance(ref, ArtifactRef):
                    raise result_error(GraphArtifactResultErrorCode.ARTIFACT_WRITE_FAILED, field="artifact.ref")
                internal_reader = getattr(
                    self._artifact_port,
                    "read_graph_result_artifact",
                    None,
                )
                stored = (
                    internal_reader(
                        ref.ref,
                        expected_run_id=request.binding.run_id,
                    )
                    if callable(internal_reader)
                    else self._artifact_port.read_artifact(ref.ref)
                )
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(GraphArtifactResultErrorCode.ARTIFACT_WRITE_FAILED, field="artifact.write") from exc
        _verify_scope(ref, request)
        _verify_candidate_payload(stored, request, error_code=GraphArtifactResultErrorCode.ARTIFACT_READBACK_FAILED)
        record = self._artifact_record(request, ref, artifact_id, artifact_type)
        verification = ArtifactVerificationReceipt.for_record(record, verified_at=self._verified_at(request.created_at))
        registration = ArtifactCatalogRegistrationRequest(
            record=record,
            verification=verification,
            initial_reference=ArtifactCatalogRegistrationRequest.from_verified_record(
                record,
                verified_at=verification.verified_at,
            ).initial_reference,
        )
        try:
            registered = self._catalog.register(registration)
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(GraphArtifactResultErrorCode.ARTIFACT_WRITE_FAILED, field="catalog.register") from exc
        if not isinstance(registered, ArtifactCatalogRegistrationResult):
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_WRITE_FAILED,
                field="catalog.registration",
            )
        canonical = registered.claim.record
        if (
            canonical.scope() != record.scope()
            or canonical.content_checksum != record.content_checksum
            or canonical.media_type != record.media_type
            or canonical.artifact_class is not record.artifact_class
        ):
            raise result_error(
                GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH,
                field="catalog.registration",
            )
        return canonical

    def _materialize_cache(
        self,
        request: NodeResultRequest,
        evaluation: PersistenceEvaluation,
    ) -> CacheRef:
        if request.dependency_digest is None:
            raise result_error(GraphArtifactResultErrorCode.CACHE_IDENTITY_INVALID, field="dependency_digest")
        expires_at = request.created_at + timedelta(seconds=self._policy.config.cache_default_ttl_seconds)
        cache_request = ResultCacheWriteRequest(
            cache_key=self._cache_key(request, evaluation),
            tenant_id=request.binding.tenant_id,
            payload=_candidate_payload(request),
            media_type=request.media_type,
            content_checksum=request.candidate_checksum,
            byte_size=request.candidate_bytes,
            dependency_digest=request.dependency_digest,
            policy_version=evaluation.decision.policy_version,
            expires_at=expires_at,
        )
        try:
            ref = self._cache.write(cache_request)
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(GraphArtifactResultErrorCode.CACHE_WRITE_FAILED, field="cache.write") from exc
        try:
            ref = reference(ref, "cache.ref")
            stored = self._cache.read(ref)
        except GraphArtifactResultError:
            raise
        except Exception as exc:
            raise result_error(GraphArtifactResultErrorCode.CACHE_READBACK_FAILED, field="cache.read") from exc
        _verify_candidate_payload(stored, request, error_code=GraphArtifactResultErrorCode.CACHE_READBACK_FAILED)
        return CacheRef(
            ref=ref,
            tenant_id=request.binding.tenant_id,
            content_checksum=request.candidate_checksum,
            dependency_digest=request.dependency_digest,
            media_type=request.media_type,
            byte_size=request.candidate_bytes,
            policy_version=evaluation.decision.policy_version,
            expires_at=expires_at,
        )

    def _artifact_record(
        self,
        request: NodeResultRequest,
        ref: ArtifactRef,
        artifact_id: str,
        artifact_type: str,
    ) -> ArtifactRecord:
        return ArtifactRecord(
            ref=ref.ref,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            content_checksum=request.candidate_checksum,
            byte_size=request.candidate_bytes,
            media_type=request.media_type,
            artifact_class=request.artifact_class,
            tenant_id=request.binding.tenant_id,
            run_id=request.binding.run_id,
            graph_id=request.binding.graph_id,
            node_id=request.binding.node_id,
            attempt_id=request.binding.attempt_id,
            producer_revision=request.provenance.producer_revision,
            sensitivity=request.sensitivity,
            reusable=request.reusable,
            dependency_digest=request.dependency_digest,
            retention_class=request.retention_class,
            expires_at=self._artifact_expiry(request),
            required_for_replay=request.required_for_replay,
            required_for_publication=request.required_for_publication,
            created_at=request.created_at,
        )

    def _artifact_expiry(self, request: NodeResultRequest) -> datetime | None:
        days = {
            "ephemeral": self._policy.config.retention.ephemeral_days,
            "run": self._policy.config.retention.run_days,
            "evidence": self._policy.config.retention.evidence_days,
            "report": self._policy.config.retention.report_days,
            "cache": self._policy.config.retention.cache_days,
        }[request.retention_class.value]
        return request.created_at + timedelta(days=days) if days is not None else None

    def _build_envelope(
        self,
        request: NodeResultRequest,
        evaluation: PersistenceEvaluation,
        *,
        records: tuple[ArtifactRecord, ...] = (),
        caches: tuple[CacheRef, ...] = (),
    ) -> NodeResultEnvelope:
        decision = evaluation.decision
        projection = (
            request.inline_projection
            if decision.mode is not PersistenceMode.OMITTED
            else {}
        )
        return NodeResultEnvelope(
            binding=request.binding,
            status=request.status,
            output_schema_ref=request.output_schema_ref,
            output_schema_digest=request.output_schema_digest,
            candidate_checksum=request.candidate_checksum,
            summary=request.summary,
            inline_projection=projection,
            materialized_refs=records,
            cache_refs=caches,
            provenance=request.provenance,
            persistence_decision=decision,
            metrics=ResultMetrics(
                candidate_bytes=request.candidate_bytes,
                candidate_tokens=request.candidate_tokens,
                summary_bytes=request.summary.byte_size,
                inline_bytes=(
                    request.inline_bytes
                    if decision.mode is PersistenceMode.INLINE
                    or (
                        decision.mode is not PersistenceMode.OMITTED
                        and bool(request.inline_projection)
                    )
                    else 0
                ),
            ),
            created_at=request.created_at,
        )

    def _omitted_evaluation(self, request: NodeResultRequest) -> PersistenceEvaluation:
        return PersistenceEvaluation(
            candidate_checksum=request.candidate_checksum,
            candidate_bytes=request.candidate_bytes,
            candidate_tokens=request.candidate_tokens,
            decision=PersistenceDecision(
                mode=PersistenceMode.OMITTED,
                reason=PersistenceReason.QUOTA_EXCEEDED,
                artifact_class=request.artifact_class,
                retention_class=request.retention_class,
                estimated_bytes=request.candidate_bytes,
                reserved_bytes=0,
                context_policy=request.context_policy,
                required=False,
                policy_version=self._policy.config.policy_version,
            ),
        )

    def _observation(self, request: NodeResultRequest, **kwargs: Any) -> ResultMaterializationObservation:
        return ResultMaterializationObservation(binding=request.binding, **kwargs)

    def _emit(self, observation: ResultMaterializationObservation) -> None:
        if self._observation_sink is None:
            return
        try:
            self._observation_sink(observation)
        except Exception:
            return

    def _verified_at(self, created_at: datetime) -> datetime:
        return max(aware_datetime(self._clock(), "clock"), created_at)

    def _quota_key(self, request: NodeResultRequest, evaluation: PersistenceEvaluation) -> str:
        return _derived_key("quota", request, evaluation)

    def _cache_key(self, request: NodeResultRequest, evaluation: PersistenceEvaluation) -> str:
        return _derived_key("cache", request, evaluation)

    def _artifact_id(self, request: NodeResultRequest) -> str:
        return _derived_identifier("result", request)

    def _artifact_type(self, request: NodeResultRequest) -> str:
        return _derived_identifier("graph-result", request)

    def _bound_run(self, run_id: str) -> AbstractContextManager[str]:
        binder = getattr(self._artifact_port, "bind_run", None)
        return binder(run_id) if callable(binder) else nullcontext(run_id)


def _candidate_payload(request: NodeResultRequest) -> dict[str, Any]:
    normalized = request.media_type
    if normalized == "application/json" or normalized.endswith("+json"):
        value = thaw_json(request.candidate)
        encoding = "json"
    elif normalized.startswith("text/"):
        value = request.candidate
        encoding = "text"
    else:
        value = base64.b64encode(request.candidate).decode("ascii")
        encoding = "base64"
    return {
        "schema": RESULT_PAYLOAD_SCHEMA,
        "candidate_checksum": request.candidate_checksum,
        "candidate_bytes": request.candidate_bytes,
        "media_type": normalized,
        "encoding": encoding,
        "value": value,
    }


def _verify_candidate_payload(
    stored: Mapping[str, Any],
    request: NodeResultRequest,
    *,
    error_code: GraphArtifactResultErrorCode,
) -> None:
    if not isinstance(stored, Mapping):
        raise result_error(error_code, field="payload")
    payload: Any = stored.get("payload", stored)
    if not isinstance(payload, Mapping):
        raise result_error(error_code, field="payload")
    try:
        payload = exact_keys(
            payload,
            required=frozenset({"schema", "candidate_checksum", "candidate_bytes", "media_type", "encoding", "value"}),
            model="GraphResultPayload",
        )
        if (
            payload["schema"] != RESULT_PAYLOAD_SCHEMA
            or payload["candidate_checksum"] != request.candidate_checksum
            or payload["candidate_bytes"] != request.candidate_bytes
            or payload["media_type"] != request.media_type
        ):
            raise ValueError("payload metadata mismatch")
        encoding = payload["encoding"]
        if encoding == "json":
            candidate = payload["value"]
        elif encoding == "text":
            candidate = payload["value"]
        elif encoding == "base64":
            candidate = base64.b64decode(payload["value"], validate=True)
        else:
            raise ValueError("payload encoding mismatch")
        _, candidate_bytes = serialize_candidate(candidate, request.media_type)
    except GraphArtifactResultError:
        raise
    except (TypeError, ValueError, base64.binascii.Error) as exc:
        raise result_error(error_code, field="payload") from exc
    if len(candidate_bytes) != request.candidate_bytes or sha256_checksum(candidate_bytes) != request.candidate_checksum:
        raise result_error(error_code, field="payload.checksum")


def _verify_scope(ref: ArtifactRef, request: NodeResultRequest) -> None:
    metadata = ref.metadata
    for field_name, expected in (
        ("tenant_id", request.binding.tenant_id),
        ("run_id", request.binding.run_id),
        ("graph_id", request.binding.graph_id),
        ("node_id", request.binding.node_id),
        ("attempt_id", request.binding.attempt_id),
    ):
        actual = metadata.get(field_name)
        if actual != expected:
            raise result_error(GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH, field=f"ref.{field_name}")


def _derived_key(prefix: str, request: NodeResultRequest, evaluation: PersistenceEvaluation) -> str:
    payload = {
        "tenant_id": request.binding.tenant_id,
        "run_id": request.binding.run_id,
        "graph_id": request.binding.graph_id,
        "node_id": request.binding.node_id,
        "attempt_id": request.binding.attempt_id,
        "candidate_checksum": request.candidate_checksum,
        "dependency_digest": request.dependency_digest,
        "policy_version": evaluation.decision.policy_version,
    }
    digest = hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"{prefix}://{request.binding.tenant_id}/{digest}"


def _quota_reconciliation_projection(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reservation_id": values["reservation_id"],
        "attempt_committed": values["attempt_committed"],
        "catalog_claim_committed": values["catalog_claim_committed"],
        "cache_entry_committed": values["cache_entry_committed"],
        "physical_operation_committed": values["physical_operation_committed"],
        "evidence_refs": list(values["evidence_refs"]),
        "observed_at": datetime_to_json(values["observed_at"]),
    }


def _derived_identifier(prefix: str, request: NodeResultRequest) -> str:
    payload = {
        "tenant_id": request.binding.tenant_id,
        "run_id": request.binding.run_id,
        "graph_id": request.binding.graph_id,
        "node_id": request.binding.node_id,
        "attempt_id": request.binding.attempt_id,
    }
    digest = hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _artifact_identity_checksum(artifact_type: str) -> str:
    prefix = "graph-result-"
    if not artifact_type.startswith(prefix):
        raise result_error(
            GraphArtifactResultErrorCode.RESULT_SCHEMA_INVALID,
            field="artifact_type",
        )
    return checksum(
        f"sha256:{artifact_type.removeprefix(prefix)}",
        "artifact.identity_checksum",
    )


def _usage_operation_id(
    request: NodeResultRequest,
    *,
    policy_version: str,
    stage: str,
) -> str:
    digest = hashlib.sha256(
        stable_json_dumps(
            {
                "binding": request.binding.to_dict(),
                "candidate_checksum": request.candidate_checksum,
                "policy_version": policy_version,
                "stage": stage,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"graph-artifact-operation://{request.binding.tenant_id}/{digest}"


def _failure_usage_kind(
    error_code: GraphArtifactResultErrorCode,
) -> GraphArtifactUsageKind | None:
    return {
        GraphArtifactResultErrorCode.ARTIFACT_READBACK_FAILED: (
            GraphArtifactUsageKind.ARTIFACT_READBACK
        ),
        GraphArtifactResultErrorCode.CACHE_WRITE_FAILED: (
            GraphArtifactUsageKind.CACHE_WRITE
        ),
        GraphArtifactResultErrorCode.CACHE_READBACK_FAILED: (
            GraphArtifactUsageKind.CACHE_READBACK
        ),
    }.get(error_code)


__all__ = [
    "MaterializationResult",
    "RESULT_PAYLOAD_SCHEMA",
    "ResultAttemptLedgerPort",
    "ResultCachePort",
    "ResultCacheWriteRequest",
    "ResultMaterializationObservation",
    "ResultMaterializationOutcome",
    "ResultMaterializer",
    "ResultQuotaPort",
    "ResultQuotaReconciliationEvidence",
    "ResultQuotaReservation",
]
