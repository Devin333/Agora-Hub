"""Harness-owned, read-only planning observations.

Planners may ask for an external fact, but they never receive a tool capability.
This module admits the request against a pinned policy, persists a bounded receipt,
and later exposes only the immutable receipt reference to candidate validation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from time import perf_counter, time_ns
from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:
    from framework.harness.ref_planning import HarnessPlanningRefAuthority

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
PLANNING_CALL_INTENT_SCHEMA = "newsroom.harness-planning-call-intent/v1"


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
    def from_task_plan_policy(cls, policy: Any, registry: ToolRegistry) -> "PlanningObservationPolicy":
        """Derive the narrow observation policy from a normalized stage policy."""

        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be ToolRegistry")
        exact_tools = []
        for name in policy.allowed_tool_ids:
            registered = registry.maybe_get(name)
            if registered is None:
                raise HarnessValidationError("planning tool binding is unavailable", code="planning_tool_unavailable")
            exact_tools.append(registered.definition.tool_id)
        return cls(
            policy_checksum=policy.policy_checksum,
            allowed_tool_ids=tuple(exact_tools),
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
        if not isinstance(value, Mapping):
            raise HarnessValidationError("planning observation receipt must be an object", code="planning_observation_receipt_corrupt")
        allowed = {
            "request", "request_checksum", "status", "reason_code", "tool_call_id",
            "observation_summary", "artifact_refs", "result_checksum", "elapsed_ms",
            "schema_version", "receipt_checksum", "source_ref",
        }
        if set(value) != allowed:
            raise HarnessValidationError("planning observation receipt fields do not match schema", code="planning_observation_receipt_corrupt")
        request_payload = value.get("request")
        if not isinstance(request_payload, Mapping):
            raise HarnessValidationError("planning observation receipt request is missing", code="planning_observation_receipt_invalid")
        request_fields = {
            "request_id", "run_id", "stage_id", "planner_turn_id", "policy_checksum",
            "correlation_id", "tool_name", "purpose", "arguments", "attempt",
            "schema_version", "request_checksum",
        }
        if set(request_payload) != request_fields:
            raise HarnessValidationError("planning observation request fields do not match schema", code="planning_observation_receipt_corrupt")
        if not isinstance(value["artifact_refs"], (list, tuple)):
            raise HarnessValidationError("planning observation artifact refs must be an array", code="planning_observation_receipt_corrupt")
        expected_request_checksum = request_payload.get("request_checksum")
        request = PlanningObservationRequest(**{key: item for key, item in request_payload.items() if key != "request_checksum"})
        if request.request_checksum != expected_request_checksum:
            raise HarnessValidationError("planning observation request checksum mismatch", code="planning_observation_receipt_corrupt")
        if value.get("request_checksum") != request.request_checksum:
            raise HarnessValidationError("planning observation receipt request checksum mismatch", code="planning_observation_receipt_corrupt")
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
    def reserve_call(self, request: PlanningObservationRequest, tool_call_id: str, max_calls: int, timeout_ms: int = 30000) -> PlanningCallIntent | None: ...
    def save(self, receipt: PlanningObservationReceipt) -> str: ...
    def by_request(self, request_checksum: str) -> PlanningObservationReceipt | None: ...
    def by_source_ref(self, source_ref: str) -> PlanningObservationReceipt | None: ...
    def receipts_for_scope(self, run_id: str, stage_id: str, planner_turn_id: str) -> tuple[PlanningObservationReceipt, ...]: ...


@dataclass(frozen=True, slots=True)
class PlanningCallIntent:
    """Durable budget consumption before execution, not evidence of success."""

    request: PlanningObservationRequest
    tool_call_id: str
    max_calls: int
    timeout_ms: int = 30000
    admitted_at_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    turn_deadline_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, PlanningObservationRequest):
            raise TypeError("request must be PlanningObservationRequest")
        identifier(self.tool_call_id, "tool_call_id")
        non_negative_int(self.max_calls, "max_calls")
        positive_int(self.timeout_ms, "timeout_ms")
        positive_int(self.admitted_at_ms, "admitted_at_ms")
        if self.turn_deadline_ms is None:
            object.__setattr__(self, "turn_deadline_ms", self.admitted_at_ms + self.timeout_ms)
        positive_int(self.turn_deadline_ms, "turn_deadline_ms")
        if not self.admitted_at_ms < self.turn_deadline_ms <= self.admitted_at_ms + self.timeout_ms:
            raise HarnessValidationError("planning intent deadline is invalid", code="planning_call_intent_corrupt")

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": PLANNING_CALL_INTENT_SCHEMA,
            "request": self.request.to_dict(),
            "tool_call_id": self.tool_call_id,
            "max_calls": self.max_calls,
            "timeout_ms": self.timeout_ms,
            "admitted_at_ms": self.admitted_at_ms,
            "turn_deadline_ms": self.turn_deadline_ms,
        }
        return {**body, "intent_checksum": canonical_payload_checksum(body)}

    def require_receipt(self, receipt: PlanningObservationReceipt) -> None:
        if receipt.request != self.request or receipt.tool_call_id != self.tool_call_id:
            raise HarnessValidationError("planning receipt differs from admitted call", code="planning_call_receipt_mismatch")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlanningCallIntent":
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version", "request", "tool_call_id", "max_calls", "intent_checksum",
            "timeout_ms", "admitted_at_ms", "turn_deadline_ms",
        } or payload["schema_version"] != PLANNING_CALL_INTENT_SCHEMA:
            raise HarnessValidationError("planning call intent schema mismatch", code="planning_call_intent_corrupt")
        body = {key: value for key, value in payload.items() if key != "intent_checksum"}
        if canonical_payload_checksum(body) != payload["intent_checksum"]:
            raise HarnessValidationError("planning call intent checksum mismatch", code="planning_call_intent_corrupt")
        raw = payload["request"]
        if not isinstance(raw, Mapping):
            raise HarnessValidationError("planning call intent request is invalid", code="planning_call_intent_corrupt")
        try:
            request = PlanningObservationRequest(**{key: value for key, value in raw.items() if key != "request_checksum"})
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError("planning call intent request is invalid", code="planning_call_intent_corrupt") from exc
        if request.to_dict() != raw:
            raise HarnessValidationError("planning call intent request mismatch", code="planning_call_intent_corrupt")
        return cls(request, payload["tool_call_id"], payload["max_calls"], payload["timeout_ms"], payload["admitted_at_ms"], payload["turn_deadline_ms"])


def admit_planning_call(
    intent: PlanningCallIntent,
    intents: tuple[PlanningCallIntent, ...],
    receipts: tuple[PlanningObservationReceipt, ...],
) -> PlanningCallIntent | None:
    """Caller owns the atomic read/check/create transaction."""
    request = intent.request
    scope = (request.run_id, request.stage_id, request.planner_turn_id)
    if any(item.request.policy_checksum != request.policy_checksum for item in receipts):
        raise HarnessValidationError("planning receipts use a different budget policy", code="planning_call_policy_conflict")
    consumed = {item.request.request_checksum for item in receipts if item.tool_call_id is not None}
    deadline = intent.turn_deadline_ms
    previous_admission = 0
    for prior in intents:
        if (prior.request.run_id, prior.request.stage_id, prior.request.planner_turn_id) != scope:
            continue
        if prior.request.policy_checksum != request.policy_checksum or prior.max_calls != intent.max_calls or prior.timeout_ms != intent.timeout_ms:
            raise HarnessValidationError("planning call budget policy changed", code="planning_call_policy_conflict")
        if prior.request.request_checksum == request.request_checksum:
            raise HarnessValidationError("planning call already admitted; reconcile its recorded outcome", code="planning_call_outcome_unconfirmed")
        consumed.add(prior.request.request_checksum)
        deadline = min(deadline, prior.turn_deadline_ms)
        previous_admission = max(previous_admission, prior.admitted_at_ms)
    if intent.admitted_at_ms < previous_admission or intent.admitted_at_ms >= deadline:
        raise HarnessValidationError("planning turn deadline exhausted or clock moved backwards", code="planning_turn_timeout")
    return replace(intent, turn_deadline_ms=deadline) if len(consumed) < intent.max_calls else None


class InMemoryPlanningObservationStore:
    """Test implementation; production code should supply a durable store port."""

    # Explicit marker lets composition roots fail closed instead of inferring
    # durability from the concrete class name.
    is_durable = False

    def __init__(self) -> None:
        self._by_request: dict[str, PlanningObservationReceipt] = {}
        self._by_source: dict[str, PlanningObservationReceipt] = {}
        self._intents: dict[str, PlanningCallIntent] = {}
        self._lock = RLock()

    def reserve_call(self, request: PlanningObservationRequest, tool_call_id: str, max_calls: int, timeout_ms: int = 30000) -> PlanningCallIntent | None:
        intent = PlanningCallIntent(request, tool_call_id, max_calls, timeout_ms)
        with self._lock:
            intent = admit_planning_call(intent, tuple(self._intents.values()), self.receipts_for_scope(request.run_id, request.stage_id, request.planner_turn_id))
            if intent is None:
                return None
            self._intents[request.request_checksum] = intent
            return intent

    def save(self, receipt: PlanningObservationReceipt) -> str:
        with self._lock:
            intent = self._intents.get(receipt.request.request_checksum)
            if intent is not None:
                intent.require_receipt(receipt)
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
    _path_locks: dict[str, Any] = {}
    _path_locks_guard = RLock()

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = Path(path).resolve()
        with self._path_locks_guard:
            self._lock = self._path_locks.setdefault(os.path.normcase(str(self._path)), RLock())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._reload()

    def _reload(self) -> None:
        self._by_request.clear()
        self._by_source.clear()
        self._intents.clear()
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    payload = json.loads(line)
                    if payload.get("schema_version") == PLANNING_CALL_INTENT_SCHEMA:
                        intent = PlanningCallIntent.from_dict(payload)
                        existing = self._intents.get(intent.request.request_checksum)
                        if existing is not None and existing != intent:
                            raise HarnessValidationError("planning call intent conflicts", code="planning_call_intent_corrupt")
                        self._intents[intent.request.request_checksum] = intent
                    else:
                        super().save(PlanningObservationReceipt.from_dict(payload))

    def reserve_call(self, request: PlanningObservationRequest, tool_call_id: str, max_calls: int, timeout_ms: int = 30000) -> PlanningCallIntent | None:
        intent = PlanningCallIntent(request, tool_call_id, max_calls, timeout_ms)
        with self._lock:
            self._reload()
            intent = admit_planning_call(intent, tuple(self._intents.values()), self.receipts_for_scope(request.run_id, request.stage_id, request.planner_turn_id))
            if intent is None:
                return None
            encoded = json.dumps(intent.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._intents[request.request_checksum] = intent
            return intent

    def by_request(self, request_checksum: str) -> PlanningObservationReceipt | None:
        with self._lock:
            self._reload()
            return super().by_request(request_checksum)

    def by_source_ref(self, source_ref: str) -> PlanningObservationReceipt | None:
        with self._lock:
            self._reload()
            return super().by_source_ref(source_ref)

    def save(self, receipt: PlanningObservationReceipt) -> str:
        with self._lock:
            self._reload()
            existing = self.by_request(receipt.request.request_checksum)
            if existing is not None:
                return super().save(receipt)
            intent = self._intents.get(receipt.request.request_checksum)
            if intent is not None:
                intent.require_receipt(receipt)
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
        planning_ref_authority: HarnessPlanningRefAuthority | None = None,
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
        if planning_ref_authority is not None:
            from framework.harness.ref_planning import HarnessPlanningRefAuthority

            if not isinstance(planning_ref_authority, HarnessPlanningRefAuthority) or planning_ref_authority.receipt_store is not store:
                raise TypeError("planning_ref_authority must own this receipt store")
            if ref_authority is not None:
                raise ValueError("planning grants cannot be combined with static authority")
            if planning_ref_authority.input_snapshot.task_policy_checksum != policy.policy_checksum:
                raise HarnessValidationError("planning policy differs from admitted authority", code="REF_POLICY_SCOPE_MISMATCH")
        self.planning_ref_authority = planning_ref_authority

    def _store_for(self, *, online: bool = False) -> PlanningObservationStorePort:
        if self.planning_ref_authority is None:
            return self._store
        return self.planning_ref_authority.reader(allow_registration=online)

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
        return getattr(self._store_for(), "is_durable", False) is True

    def observe(self, request: PlanningObservationRequest) -> PlanningObservationReceipt:
        if self.planning_ref_authority is not None:
            self.planning_ref_authority.require_scope(request.run_id, request.stage_id, request.policy_checksum, request.planner_turn_id)
        if self._ref_authority is not None:
            self._ref_authority.require_scope(self._ref_policy, run_id=request.run_id, stage_id=request.stage_id)
        store = self._store_for(online=True)
        existing = store.by_request(request.request_checksum)
        if existing is not None:
            if existing.request != request:
                raise HarnessValidationError("planning request differs from recorded receipt", code="planning_observation_receipt_scope_mismatch")
            return PlanningObservationReceipt.from_dict(existing.to_dict())
        reason = self._admission_reason(request)
        if reason is not None:
            return self._persist(PlanningObservationReceipt(request=request, status="REJECTED", reason_code=reason))
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
        intent = store.reserve_call(request, call.call_id, self._policy.max_tool_calls, self._policy.timeout_seconds * 1000)
        if intent is None:
            return self._persist(PlanningObservationReceipt(request=request, status="REJECTED", reason_code="planning_tool_budget_exhausted"))
        remaining_ms = intent.turn_deadline_ms - time_ns() // 1_000_000
        if remaining_ms <= 0:
            return self._persist(PlanningObservationReceipt(request=request, status="TIMED_OUT", reason_code="planning_turn_timeout", tool_call_id=call.call_id))
        effective_timeout = remaining_ms / 1000
        started_at = perf_counter()
        try:
            observation = self._executor.execute(
                call,
                ToolPolicy(
                    allowed_tools=[request.tool_name],
                    require_explicit_allowlist=True,
                    allow_network_access=False,
                    allow_dangerous_tools=False,
                    require_approval_for_side_effects=True,
                    default_timeout_seconds=effective_timeout,
                    timeout_seconds_default=effective_timeout,
                    max_tool_calls_per_iteration=1,
                    max_tool_calls_per_agent=1,
                ),
            )
        except Exception:
            elapsed_ms = max(1, int((perf_counter() - started_at) * 1000))
            timed_out = elapsed_ms > remaining_ms
            return self._persist(
                PlanningObservationReceipt(
                    request=request,
                    status="TIMED_OUT" if timed_out else "FAILED",
                    reason_code="planning_tool_timeout" if timed_out else "planning_tool_execution_failed",
                    tool_call_id=call.call_id,
                    elapsed_ms=elapsed_ms,
                )
            )
        elapsed_ms = max(1, int((perf_counter() - started_at) * 1000))
        if elapsed_ms > remaining_ms:
            return self._persist(PlanningObservationReceipt(
                request=request, status="TIMED_OUT", reason_code="planning_tool_timeout",
                tool_call_id=call.call_id, elapsed_ms=elapsed_ms,
            ))
        if not isinstance(observation, ToolObservation) or observation.call != call:
            return self._persist(PlanningObservationReceipt(
                request=request, status="FAILED", reason_code="planning_tool_observation_mismatch",
                tool_call_id=call.call_id, elapsed_ms=elapsed_ms,
            ))
        try:
            receipt = _receipt_from_observation(request, observation, effective_timeout)
        except (HarnessValidationError, TypeError, ValueError):
            receipt = PlanningObservationReceipt(
                request=request, status="FAILED", reason_code="planning_tool_observation_invalid",
                tool_call_id=call.call_id, elapsed_ms=elapsed_ms,
            )
        return self._persist(receipt)

    def replay(self, request: PlanningObservationRequest) -> PlanningObservationReceipt:
        """Return recorded evidence only. This method must never invoke the executor."""

        if self.planning_ref_authority is not None:
            self.planning_ref_authority.require_scope(request.run_id, request.stage_id, request.policy_checksum, request.planner_turn_id)
        if self._ref_authority is not None:
            self._ref_authority.require_scope(self._ref_policy, run_id=request.run_id, stage_id=request.stage_id)
        receipt = self._store_for().by_request(request.request_checksum)
        if receipt is None:
            raise HarnessValidationError("planning observation receipt is unavailable for replay", code="planning_observation_receipt_missing")
        if receipt.request != request:
            raise HarnessValidationError("planning observation replay request does not match receipt", code="planning_observation_receipt_scope_mismatch")
        return PlanningObservationReceipt.from_dict(receipt.to_dict())

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
        if policy_checksum != self._policy.policy_checksum:
            raise HarnessValidationError("planning observation policy differs from candidate policy", code="planning_policy_checksum_mismatch")
        if self.planning_ref_authority is not None:
            self.planning_ref_authority.require_scope(run_id, stage_id, policy_checksum, planner_turn_id)
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
            receipt = self._store_for().by_source_ref(source_ref)
            if receipt is None:
                raise HarnessValidationError("planning observation receipt is missing", code="planning_observation_receipt_missing", details={"source_ref": source_ref})
            if receipt.source_ref != source_ref:
                raise HarnessValidationError(
                    "planning observation store returned a mismatched receipt",
                    code="planning_observation_receipt_corrupt",
                    details={"source_ref": source_ref},
                )
            receipt = PlanningObservationReceipt.from_dict(receipt.to_dict())
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
        store = self._store_for(online=True)
        if store.save(receipt) != receipt.receipt_checksum:
            raise HarnessValidationError("planning store returned a different receipt", code="planning_observation_receipt_corrupt")
        recorded = store.by_request(receipt.request.request_checksum)
        if recorded is None or recorded != receipt:
            raise HarnessValidationError("planning store did not retain the committed receipt", code="planning_observation_receipt_corrupt")
        return PlanningObservationReceipt.from_dict(recorded.to_dict())

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
    timeout_seconds: float,
) -> PlanningObservationReceipt:
    elapsed_ms = max(1, int(round(observation.elapsed_ms)))
    if observation.status is ToolStatus.TIMEOUT or elapsed_ms > timeout_seconds * 1000:
        return PlanningObservationReceipt(
            request=request,
            status="TIMED_OUT",
            reason_code="planning_tool_timeout",
            tool_call_id=observation.tool_call_id,
            elapsed_ms=elapsed_ms,
        )
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
