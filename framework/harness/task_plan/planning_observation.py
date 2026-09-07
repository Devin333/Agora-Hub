"""Harness-owned, read-only planning observations.

Planners may ask for an external fact, but they never receive a tool capability.
This module admits the request against a pinned policy, persists a bounded receipt,
and later exposes only the immutable receipt reference to candidate validation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    REF_KIND_PLANNING,
    REF_KIND_RESULT,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    RefResolutionPort,
    normalize_ref_descriptors,
    validate_ref_configuration,
)
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    frozen_mapping,
    identifier,
    non_negative_int,
    positive_int,
    reference,
    required_text,
    stable_text_tuple,
    thaw_mapping,
)
from framework.tool import ToolCall, ToolExecutor, ToolObservation, ToolPolicy, ToolRegistry
from framework.tool.models import ToolSideEffect, ToolStatus


PLANNING_OBSERVATION_REQUEST_SCHEMA = "newsroom.harness-planning-observation-request/v1"
PLANNING_OBSERVATION_RECEIPT_SCHEMA = "newsroom.harness-planning-observation-receipt/v1"


@dataclass(frozen=True, slots=True)
class PlanningObservationPolicy:
    """Pinned Harness policy for planning-only read operations."""

    policy_checksum: str
    allowed_tool_ids: tuple[str, ...] = ()
    max_tool_calls: int = 0
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_checksum", checksum(self.policy_checksum, "policy_checksum"))
        object.__setattr__(
            self,
            "allowed_tool_ids",
            stable_text_tuple(self.allowed_tool_ids, "allowed_tool_ids", item_kind="exact_reference"),
        )
        object.__setattr__(self, "max_tool_calls", non_negative_int(self.max_tool_calls, "max_tool_calls"))
        object.__setattr__(self, "timeout_seconds", positive_int(self.timeout_seconds, "timeout_seconds"))

    @classmethod
    def from_task_plan_policy(cls, policy: Any) -> "PlanningObservationPolicy":
        """Derive the narrow observation policy from a normalized stage policy."""

        return cls(
            policy_checksum=policy.policy_checksum,
            allowed_tool_ids=tuple(policy.allowed_tool_ids),
            max_tool_calls=policy.max_planning_tool_calls,
            timeout_seconds=policy.planning_timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class PlanningObservationRequest:
    """A candidate request, deliberately unable to select grants or budgets."""

    request_id: str
    run_id: str
    stage_id: str
    planner_turn_id: str
    policy_checksum: str
    correlation_id: str
    tool_name: str
    purpose: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    attempt: int = 1
    schema_version: str = PLANNING_OBSERVATION_REQUEST_SCHEMA
    request_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PLANNING_OBSERVATION_REQUEST_SCHEMA:
            raise HarnessValidationError(
                "planning observation request schema is unsupported",
                code="planning_observation_schema_unsupported",
            )
        for field_name in (
            "request_id",
            "run_id",
            "stage_id",
            "planner_turn_id",
            "correlation_id",
        ):
            object.__setattr__(self, field_name, identifier(getattr(self, field_name), field_name))
        object.__setattr__(self, "policy_checksum", checksum(self.policy_checksum, "policy_checksum"))
        object.__setattr__(self, "tool_name", required_text(self.tool_name, "tool_name"))
        if "." not in self.tool_name:
            raise HarnessValidationError(
                "planning observation tool must be namespaced",
                code="planning_tool_invalid",
            )
        object.__setattr__(self, "purpose", required_text(self.purpose, "purpose", max_length=1024))
        object.__setattr__(self, "arguments", frozen_mapping(self.arguments, "planning_observation.arguments"))
        object.__setattr__(self, "attempt", positive_int(self.attempt, "attempt"))
        object.__setattr__(self, "request_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "planner_turn_id": self.planner_turn_id,
            "policy_checksum": self.policy_checksum,
            "correlation_id": self.correlation_id,
            "tool_name": self.tool_name,
            "purpose": self.purpose,
            "arguments": thaw_mapping(self.arguments),
            "attempt": self.attempt,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "request_checksum": self.request_checksum}


@dataclass(frozen=True, slots=True)
class PlanningObservationReceipt:
    """Durable evidence of an admitted planning observation, never raw output."""

    request: PlanningObservationRequest
    status: str
    reason_code: str | None = None
    tool_call_id: str | None = None
    observation_summary: str | None = None
    artifact_refs: tuple[str, ...] = ()
    result_checksum: str | None = None
    elapsed_ms: int | None = None
    schema_version: str = PLANNING_OBSERVATION_RECEIPT_SCHEMA
    receipt_checksum: str = field(init=False)
    source_ref: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, PlanningObservationRequest):
            raise TypeError("request must be PlanningObservationRequest")
        if self.schema_version != PLANNING_OBSERVATION_RECEIPT_SCHEMA:
            raise HarnessValidationError(
                "planning observation receipt schema is unsupported",
                code="planning_observation_schema_unsupported",
            )
        if self.status not in {"SUCCEEDED", "REJECTED", "FAILED", "TIMED_OUT"}:
            raise HarnessValidationError(
                "planning observation receipt has an invalid status",
                code="planning_observation_receipt_invalid",
            )
        if self.status == "SUCCEEDED" and (self.reason_code is not None or self.result_checksum is None):
            raise HarnessValidationError(
                "successful planning observation requires result evidence only",
                code="planning_observation_receipt_invalid",
            )
        if self.status != "SUCCEEDED" and not self.reason_code:
            raise HarnessValidationError(
                "unsuccessful planning observation requires a reason code",
                code="planning_observation_receipt_invalid",
            )
        object.__setattr__(self, "reason_code", required_text(self.reason_code, "reason_code") if self.reason_code else None)
        object.__setattr__(self, "tool_call_id", identifier(self.tool_call_id, "tool_call_id") if self.tool_call_id else None)
        object.__setattr__(self, "observation_summary", required_text(self.observation_summary, "observation_summary", max_length=4096) if self.observation_summary else None)
        object.__setattr__(self, "artifact_refs", stable_text_tuple(self.artifact_refs, "artifact_refs", item_kind="reference"))
        object.__setattr__(self, "result_checksum", checksum(self.result_checksum, "result_checksum") if self.result_checksum else None)
        if self.elapsed_ms is not None:
            object.__setattr__(self, "elapsed_ms", positive_int(self.elapsed_ms, "elapsed_ms"))
        object.__setattr__(self, "receipt_checksum", canonical_payload_checksum(self.checksum_projection()))
        object.__setattr__(self, "source_ref", f"planning-observation://{self.receipt_checksum}")

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_checksum": self.request.request_checksum,
            "status": self.status,
            "reason_code": self.reason_code,
            "tool_call_id": self.tool_call_id,
            "observation_summary": self.observation_summary,
            "artifact_refs": list(self.artifact_refs),
            "result_checksum": self.result_checksum,
            "elapsed_ms": self.elapsed_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            **self.checksum_projection(),
            "receipt_checksum": self.receipt_checksum,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanningObservationReceipt":
        request_payload = value.get("request")
        if not isinstance(request_payload, Mapping):
            raise HarnessValidationError("planning observation receipt request is missing", code="planning_observation_receipt_invalid")
        expected_request_checksum = request_payload.get("request_checksum")
        request = PlanningObservationRequest(**{key: item for key, item in request_payload.items() if key != "request_checksum"})
        if request.request_checksum != expected_request_checksum:
            raise HarnessValidationError("planning observation request checksum mismatch", code="planning_observation_receipt_corrupt")
        receipt = cls(
            request=request,
            status=value.get("status"),
            reason_code=value.get("reason_code"),
            tool_call_id=value.get("tool_call_id"),
            observation_summary=value.get("observation_summary"),
            artifact_refs=tuple(value.get("artifact_refs") or ()),
            result_checksum=value.get("result_checksum"),
            elapsed_ms=value.get("elapsed_ms"),
            schema_version=value.get("schema_version", PLANNING_OBSERVATION_RECEIPT_SCHEMA),
        )
        if receipt.receipt_checksum != value.get("receipt_checksum") or receipt.source_ref != value.get("source_ref"):
            raise HarnessValidationError("planning observation receipt checksum mismatch", code="planning_observation_receipt_corrupt")
        return receipt


@runtime_checkable
class PlanningObservationStorePort(Protocol):
    def save(self, receipt: PlanningObservationReceipt) -> str: ...
    def by_request(self, request_checksum: str) -> PlanningObservationReceipt | None: ...
    def by_source_ref(self, source_ref: str) -> PlanningObservationReceipt | None: ...
    def receipts_for_scope(self, run_id: str, stage_id: str, planner_turn_id: str) -> tuple[PlanningObservationReceipt, ...]: ...


class InMemoryPlanningObservationStore:
    """Test implementation; production code should supply a durable store port."""

    # Explicit marker lets composition roots fail closed instead of inferring
    # durability from the concrete class name.
    is_durable = False

    def __init__(self) -> None:
        self._by_request: dict[str, PlanningObservationReceipt] = {}
        self._by_source: dict[str, PlanningObservationReceipt] = {}
        self._lock = RLock()

    def save(self, receipt: PlanningObservationReceipt) -> str:
        with self._lock:
            existing = self._by_request.get(receipt.request.request_checksum)
            if existing is not None:
                if existing.receipt_checksum != receipt.receipt_checksum:
                    raise HarnessValidationError("planning observation request is not idempotent", code="planning_observation_idempotency_conflict")
                return existing.receipt_checksum
            self._by_request[receipt.request.request_checksum] = receipt
            self._by_source[receipt.source_ref] = receipt
            return receipt.receipt_checksum

    def by_request(self, request_checksum: str) -> PlanningObservationReceipt | None:
        return self._by_request.get(checksum(request_checksum, "request_checksum"))

    def by_source_ref(self, source_ref: str) -> PlanningObservationReceipt | None:
        return self._by_source.get(reference(source_ref, "source_ref"))

    def receipts_for_scope(self, run_id: str, stage_id: str, planner_turn_id: str) -> tuple[PlanningObservationReceipt, ...]:
        return tuple(
            sorted(
                (
                    receipt
                    for receipt in self._by_request.values()
                    if receipt.request.run_id == run_id
                    and receipt.request.stage_id == stage_id
                    and receipt.request.planner_turn_id == planner_turn_id
                ),
                key=lambda item: (item.request.attempt, item.request.request_id),
            )
        )


class JsonlPlanningObservationStore(InMemoryPlanningObservationStore):
    """Append-only durable receipt store used where no project event store is bound."""

    is_durable = True

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    super().save(PlanningObservationReceipt.from_dict(json.loads(line)))

    def save(self, receipt: PlanningObservationReceipt) -> str:
        with self._lock:
            existing = self.by_request(receipt.request.request_checksum)
            if existing is not None:
                return super().save(receipt)
            encoded = json.dumps(receipt.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            return super().save(receipt)


@runtime_checkable
class PlanningObservationPort(Protocol):
    def observe(self, request: PlanningObservationRequest) -> PlanningObservationReceipt: ...
    def replay(self, request: PlanningObservationRequest) -> PlanningObservationReceipt: ...
    def validate_source_refs(
        self,
        source_observation_refs: tuple[str, ...],
        *,
        run_id: str,
        stage_id: str,
        planner_turn_id: str,
        policy_checksum: str,
    ) -> tuple[PlanningObservationReceipt, ...]: ...


class HarnessPlanningObservationService:
    """Admission owner for planning tools; replay is intentionally read-only."""

    def __init__(
        self,
        *,
        executor: ToolExecutor,
        registry: ToolRegistry,
        store: PlanningObservationStorePort,
        policy: PlanningObservationPolicy,
        ref_authority: RefAuthority | None = None,
        ref_policy: RefAccessPolicy | None = None,
        ref_resolution: RefResolutionPort | None = None,
        ref_descriptors: Mapping[str, RefDescriptor] | None = None,
    ) -> None:
        if not isinstance(executor, ToolExecutor):
            raise TypeError("executor must be ToolExecutor")
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be ToolRegistry")
        if not isinstance(store, PlanningObservationStorePort):
            raise TypeError("store must implement PlanningObservationStorePort")
        if not isinstance(policy, PlanningObservationPolicy):
            raise TypeError("policy must be PlanningObservationPolicy")
        normalized_descriptors = normalize_ref_descriptors(ref_descriptors)
        validate_ref_configuration(
            ref_authority,
            ref_policy,
            ref_resolution,
            normalized_descriptors,
        )
        self._executor = executor
        self._registry = registry
        self._store = store
        self._policy = policy
        self._ref_authority = ref_authority
        self._ref_policy = ref_policy
        self._ref_resolution = ref_resolution
        self._ref_descriptors = normalized_descriptors

    @property
    def store(self) -> PlanningObservationStorePort:
        """Return the receipt store for composition-time durability checks."""

        return self._store

    @property
    def policy(self) -> PlanningObservationPolicy:
        """Return the immutable planning policy bound to this service."""

        return self._policy

    @property
    def policy_checksum(self) -> str:
        return self._policy.policy_checksum

    @property
    def is_durable(self) -> bool:
        return getattr(self._store, "is_durable", False) is True

    def observe(self, request: PlanningObservationRequest) -> PlanningObservationReceipt:
        if self._ref_authority is not None:
            self._ref_authority.require_scope(self._ref_policy, run_id=request.run_id, stage_id=request.stage_id)
        existing = self._store.by_request(request.request_checksum)
        if existing is not None:
            return existing
        reason = self._admission_reason(request)
        if reason is not None:
            return self._persist(PlanningObservationReceipt(request=request, status="REJECTED", reason_code=reason))
        prior_calls = sum(
            1
            for item in self._store.receipts_for_scope(request.run_id, request.stage_id, request.planner_turn_id)
            if item.tool_call_id is not None
        )
        if prior_calls >= self._policy.max_tool_calls:
            return self._persist(PlanningObservationReceipt(request=request, status="REJECTED", reason_code="planning_tool_budget_exhausted"))
        call = ToolCall.new(
            request.tool_name,
            thaw_mapping(request.arguments),
            requested_by="harness.planning_observation",
            metadata={
                "planning_request_checksum": request.request_checksum,
                "planning_correlation_id": request.correlation_id,
                "planner_turn_id": request.planner_turn_id,
            },
        )
        try:
            observation = self._executor.execute(
                call,
                ToolPolicy(
                    allowed_tools=[request.tool_name],
                    require_explicit_allowlist=True,
                    allow_network_access=False,
                    allow_dangerous_tools=False,
                    require_approval_for_side_effects=True,
                    default_timeout_seconds=float(self._policy.timeout_seconds),
                    timeout_seconds_default=float(self._policy.timeout_seconds),
                    max_tool_calls_per_iteration=1,
                    max_tool_calls_per_agent=1,
                ),
            )
        except Exception:
            return self._persist(
                PlanningObservationReceipt(
                    request=request,
                    status="FAILED",
                    reason_code="planning_tool_execution_failed",
                )
            )
        return self._persist(_receipt_from_observation(request, observation, self._policy.timeout_seconds))

    def replay(self, request: PlanningObservationRequest) -> PlanningObservationReceipt:
        """Return recorded evidence only. This method must never invoke the executor."""

        if self._ref_authority is not None:
            self._ref_authority.require_scope(self._ref_policy, run_id=request.run_id, stage_id=request.stage_id)
        receipt = self._store.by_request(request.request_checksum)
        if receipt is None:
            raise HarnessValidationError("planning observation receipt is unavailable for replay", code="planning_observation_receipt_missing")
        if receipt.request != request:
            raise HarnessValidationError("planning observation replay request does not match receipt", code="planning_observation_receipt_scope_mismatch")
        return receipt

    def validate_source_refs(
        self,
        source_observation_refs: tuple[str, ...],
        *,
        run_id: str,
        stage_id: str,
        planner_turn_id: str,
        policy_checksum: str,
    ) -> tuple[PlanningObservationReceipt, ...]:
        """Fail closed before plan acceptance when a candidate cites stale evidence."""

        refs = stable_text_tuple(source_observation_refs, "source_observation_refs", item_kind="reference")
        if self._ref_authority is not None:
            self._ref_authority.require_scope(
                self._ref_policy,
                run_id=run_id,
                stage_id=stage_id,
            )
        receipts: list[PlanningObservationReceipt] = []
        for source_ref in refs:
            descriptor = None
            if self._ref_authority is not None:
                descriptor = self._ref_authority.authorize_ref(
                    source_ref,
                    self._ref_policy,
                    resolver=self._ref_resolution,
                    descriptors=self._ref_descriptors,
                    expected_kind=REF_KIND_PLANNING,
                )
            receipt = self._store.by_source_ref(source_ref)
            if receipt is None:
                raise HarnessValidationError("planning observation receipt is missing", code="planning_observation_receipt_missing", details={"source_ref": source_ref})
            if receipt.source_ref != source_ref:
                raise HarnessValidationError(
                    "planning observation store returned a mismatched receipt",
                    code="planning_observation_receipt_corrupt",
                    details={"source_ref": source_ref},
                )
            request = receipt.request
            if (request.run_id, request.stage_id, request.planner_turn_id, request.policy_checksum) != (run_id, stage_id, planner_turn_id, policy_checksum):
                raise HarnessValidationError("planning observation receipt is outside candidate scope", code="planning_observation_receipt_scope_mismatch", details={"source_ref": source_ref})
            if receipt.status != "SUCCEEDED":
                raise HarnessValidationError("planning observation receipt is not successful", code="planning_observation_receipt_unusable", details={"source_ref": source_ref, "reason_code": receipt.reason_code})
            if self._ref_authority is not None:
                if descriptor.source_checksum != receipt.receipt_checksum:
                    raise HarnessValidationError("planning source checksum does not match the stored receipt", code="REF_CHECKSUM_MISMATCH")
                for artifact_ref in receipt.artifact_refs:
                    self._ref_authority.authorize_ref(
                        artifact_ref,
                        self._ref_policy,
                        resolver=self._ref_resolution,
                        descriptors=self._ref_descriptors,
                        expected_kind=REF_KIND_RESULT,
                    )
            receipts.append(receipt)
        return tuple(receipts)

    def _persist(self, receipt: PlanningObservationReceipt) -> PlanningObservationReceipt:
        self._store.save(receipt)
        return receipt

    def _admission_reason(self, request: PlanningObservationRequest) -> str | None:
        if request.policy_checksum != self._policy.policy_checksum:
            return "planning_policy_checksum_mismatch"
        registered = self._registry.maybe_get(request.tool_name)
        if registered is None:
            return "planning_tool_unavailable"
        definition = registered.definition
        if definition.tool_id not in self._policy.allowed_tool_ids:
            return "planning_tool_not_allowlisted"
        if definition.side_effect_value != ToolSideEffect.READ_ONLY.value:
            return "planning_tool_not_read_only"
        if definition.is_dangerous or definition.requires_approval:
            return "planning_tool_requires_approval"
        return None


def _receipt_from_observation(
    request: PlanningObservationRequest,
    observation: ToolObservation,
    timeout_seconds: int,
) -> PlanningObservationReceipt:
    elapsed_ms = max(1, int(round(observation.elapsed_ms)))
    if observation.status is ToolStatus.SUCCEEDED:
        result_checksum = canonical_payload_checksum(observation.to_dict())
        return PlanningObservationReceipt(
            request=request,
            status="SUCCEEDED",
            tool_call_id=observation.tool_call_id,
            observation_summary=observation.summary,
            artifact_refs=tuple(item.uri for item in observation.result.artifact_refs),
            result_checksum=result_checksum,
            elapsed_ms=elapsed_ms,
        )
    if observation.status is ToolStatus.TIMEOUT or elapsed_ms > timeout_seconds * 1000:
        return PlanningObservationReceipt(
            request=request,
            status="TIMED_OUT",
            reason_code="planning_tool_timeout",
            tool_call_id=observation.tool_call_id,
            elapsed_ms=elapsed_ms,
        )
    return PlanningObservationReceipt(
        request=request,
        status="FAILED",
        reason_code="planning_tool_failed",
        tool_call_id=observation.tool_call_id,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "PLANNING_OBSERVATION_RECEIPT_SCHEMA",
    "PLANNING_OBSERVATION_REQUEST_SCHEMA",
    "HarnessPlanningObservationService",
    "InMemoryPlanningObservationStore",
    "JsonlPlanningObservationStore",
    "PlanningObservationPolicy",
    "PlanningObservationPort",
    "PlanningObservationReceipt",
    "PlanningObservationRequest",
    "PlanningObservationStorePort",
]
