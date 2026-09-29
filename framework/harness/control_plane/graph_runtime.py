from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from framework.events.errors import EventReplayMismatchError, EventStoreCorruptionError
from framework.events.graph_phase import GraphPhaseTransitionRecord
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.decision import (
    HarnessGraphDecision,
    HarnessGraphDecisionType,
)
from framework.harness.control_plane.graph_evaluator import (
    HarnessAcceptedGraphObservation,
    HarnessGraphObservationType,
)
from framework.harness.graph.result_lineage import (
    HarnessGraphResultLineage,
    HarnessGraphResultLineageStatus,
)
from framework.harness.control_plane.graph_state import (
    HarnessBudgetCounterState,
    HarnessGraphBudgetState,
    HarnessGraphState,
)
from framework.harness.control_plane.state import HarnessRunSpec
from framework.harness.graph.canonical import (
    canonical_checksum,
    freeze_json,
    required_text,
    thaw_json,
)
from framework.harness.graph.model import (
    HarnessContractKind,
    HarnessContractReference,
    NormalizedHarnessGraph,
)
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.graph.validation import HarnessGraphPreflightPolicy
from framework.harness.graph.versioning import (
    GRAPH_ONLY_HARNESS_GRAPH_STATE_SCHEMA,
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_GRAPH_RUNTIME_VERSION,
)
from framework.shared.time import ensure_utc, format_datetime


HARNESS_GRAPH_ACTIVITY_SCHEMA = "newsroom.harness-graph-activity/v1"
HARNESS_GRAPH_ACTIVITY_RESULT_SCHEMA = "newsroom.harness-graph-activity-result/v1"
HARNESS_GRAPH_COMMIT_SCHEMA = "newsroom.harness-graph-control-commit/v1"
HARNESS_GRAPH_PROJECTION_RECORD_SCHEMA = (
    "newsroom.harness-graph-projection-record/v2"
)
_CHECKSUM_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_GRAPH_SCOPE_FIELDS = (
    "tenant_scope_ref",
    "identity_scope_ref",
    "subject_scope_ref",
)


class HarnessGraphCommitKind(StrEnum):
    INITIALIZE = "initialize"
    DECISION = "decision"
    DECISION_PROJECTION = "decision_projection"
    ACTIVITY_RESULT = "activity_result"
    ACTIVITY_RESULT_PROJECTION = "activity_result_projection"
    OBSERVATION = "observation"
    OBSERVATION_PROJECTION = "observation_projection"


class HarnessGraphActivityResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class HarnessGraphActivity:
    run_id: str
    graph_ref: HarnessGraphReference
    node_id: str
    node_instance_id: str
    step_ref: HarnessContractReference
    worker_ref: HarnessContractReference
    activity_ref: HarnessContractReference
    attempt: int
    input_ref: str
    causal_decision_checksum: str
    causal_decision_sequence: int
    fencing_generation: int
    tenant_scope_ref: str | None = None
    identity_scope_ref: str | None = None
    subject_scope_ref: str | None = None
    schema_version: str = HARNESS_GRAPH_ACTIVITY_SCHEMA
    activity_id: str = field(init=False)
    idempotency_key: str = field(init=False)
    activity_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        run_id = required_text(self.run_id, "graph_activity.run_id")
        if not isinstance(self.graph_ref, HarnessGraphReference):
            raise TypeError("graph_ref must be HarnessGraphReference")
        node_id = required_text(self.node_id, "graph_activity.node_id")
        node_instance_id = required_text(
            self.node_instance_id,
            "graph_activity.node_instance_id",
        )
        _require_contract_kind(
            self.step_ref,
            HarnessContractKind.STEP,
            "graph_activity.step_ref",
        )
        _require_contract_kind(
            self.worker_ref,
            HarnessContractKind.WORKER,
            "graph_activity.worker_ref",
        )
        _require_contract_kind(
            self.activity_ref,
            HarnessContractKind.ACTIVITY,
            "graph_activity.activity_ref",
        )
        _positive_int(self.attempt, "graph_activity.attempt")
        input_ref = _checksum(self.input_ref, "graph_activity.input_ref")
        decision_checksum = _checksum(
            self.causal_decision_checksum,
            "graph_activity.causal_decision_checksum",
        )
        _positive_int(
            self.causal_decision_sequence,
            "graph_activity.causal_decision_sequence",
        )
        _nonnegative_int(
            self.fencing_generation,
            "graph_activity.fencing_generation",
        )
        scopes = {
            field_name: _optional_checksum(getattr(self, field_name), field_name)
            for field_name in _GRAPH_SCOPE_FIELDS
        }
        if self.schema_version != HARNESS_GRAPH_ACTIVITY_SCHEMA:
            raise HarnessValidationError(
                "unsupported graph activity schema",
                code="unsupported_graph_activity_schema",
            )
        identity = {
            "schema_version": self.schema_version,
            "run_id": run_id,
            "graph_checksum": self.graph_ref.checksum,
            "node_id": node_id,
            "node_instance_id": node_instance_id,
            "attempt": self.attempt,
            "activity_ref": self.activity_ref.exact_ref,
            "causal_decision_checksum": decision_checksum,
            "fencing_generation": self.fencing_generation,
            **scopes,
        }
        digest = canonical_checksum(identity).removeprefix("sha256:")
        activity_id = f"hga_{digest}"
        logical_identity = {
            "schema_version": self.schema_version,
            "run_id": run_id,
            "graph_checksum": self.graph_ref.checksum,
            "node_id": node_id,
            "node_instance_id": node_instance_id,
            "step_ref": self.step_ref.exact_ref,
            "worker_ref": self.worker_ref.exact_ref,
            "activity_ref": self.activity_ref.exact_ref,
            "input_ref": input_ref,
            **scopes,
        }
        logical_digest = canonical_checksum(logical_identity).removeprefix("sha256:")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "node_instance_id", node_instance_id)
        object.__setattr__(self, "input_ref", input_ref)
        object.__setattr__(self, "causal_decision_checksum", decision_checksum)
        for field_name, value in scopes.items():
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "activity_id", activity_id)
        object.__setattr__(self, "idempotency_key", f"hga_idem_{logical_digest}")
        object.__setattr__(
            self,
            "activity_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "graph_ref": self.graph_ref.to_dict(),
            "node_id": self.node_id,
            "node_instance_id": self.node_instance_id,
            "step_ref": self.step_ref.to_dict(),
            "worker_ref": self.worker_ref.to_dict(),
            "activity_ref": self.activity_ref.to_dict(),
            "attempt": self.attempt,
            "input_ref": self.input_ref,
            "causal_decision_checksum": self.causal_decision_checksum,
            "causal_decision_sequence": self.causal_decision_sequence,
            "fencing_generation": self.fencing_generation,
            "tenant_scope_ref": self.tenant_scope_ref,
            "identity_scope_ref": self.identity_scope_ref,
            "subject_scope_ref": self.subject_scope_ref,
            "activity_id": self.activity_id,
            "idempotency_key": self.idempotency_key,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "activity_checksum": self.activity_checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HarnessGraphActivity:
        expected = {
            "schema_version",
            "run_id",
            "graph_ref",
            "node_id",
            "node_instance_id",
            "step_ref",
            "worker_ref",
            "activity_ref",
            "attempt",
            "input_ref",
            "causal_decision_checksum",
            "causal_decision_sequence",
            "fencing_generation",
            "tenant_scope_ref",
            "identity_scope_ref",
            "subject_scope_ref",
            "activity_id",
            "idempotency_key",
            "activity_checksum",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise HarnessValidationError(
                "graph activity fields are invalid",
                code="graph_activity_fields_invalid",
            )
        graph_ref = value["graph_ref"]
        step_ref = value["step_ref"]
        worker_ref = value["worker_ref"]
        activity_ref = value["activity_ref"]
        if not all(
            isinstance(item, Mapping)
            for item in (graph_ref, step_ref, worker_ref, activity_ref)
        ):
            raise HarnessValidationError(
                "graph activity references must be objects",
                code="graph_activity_fields_invalid",
            )
        activity = cls(
            run_id=value["run_id"],
            graph_ref=HarnessGraphReference.from_dict(graph_ref),
            node_id=value["node_id"],
            node_instance_id=value["node_instance_id"],
            step_ref=HarnessContractReference.from_dict(step_ref),
            worker_ref=HarnessContractReference.from_dict(worker_ref),
            activity_ref=HarnessContractReference.from_dict(activity_ref),
            attempt=value["attempt"],
            input_ref=value["input_ref"],
            causal_decision_checksum=value["causal_decision_checksum"],
            causal_decision_sequence=value["causal_decision_sequence"],
            fencing_generation=value["fencing_generation"],
            tenant_scope_ref=value["tenant_scope_ref"],
            identity_scope_ref=value["identity_scope_ref"],
            subject_scope_ref=value["subject_scope_ref"],
            schema_version=value["schema_version"],
        )
        if (
            value["activity_id"] != activity.activity_id
            or value["idempotency_key"] != activity.idempotency_key
            or value["activity_checksum"] != activity.activity_checksum
        ):
            raise HarnessValidationError(
                "graph activity checksum or deterministic identity is invalid",
                code="graph_activity_checksum_invalid",
            )
        return activity


@dataclass(frozen=True, slots=True)
class HarnessGraphActivityResult:
    activity_id: str
    node_instance_id: str
    attempt: int
    idempotency_key: str
    fencing_generation: int
    activity_ref: HarnessContractReference
    evidence_ref: str
    payload_ref: str
    status: HarnessGraphActivityResultStatus | str
    tenant_scope_ref: str | None = None
    identity_scope_ref: str | None = None
    subject_scope_ref: str | None = None
    result_lineage: HarnessGraphResultLineage | None = None
    schema_version: str = HARNESS_GRAPH_ACTIVITY_RESULT_SCHEMA
    termination_confirmed: bool | None = None
    result_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "activity_id",
            required_text(self.activity_id, "graph_activity_result.activity_id"),
        )
        object.__setattr__(
            self,
            "node_instance_id",
            required_text(
                self.node_instance_id,
                "graph_activity_result.node_instance_id",
            ),
        )
        _positive_int(self.attempt, "graph_activity_result.attempt")
        object.__setattr__(
            self,
            "idempotency_key",
            required_text(
                self.idempotency_key,
                "graph_activity_result.idempotency_key",
            ),
        )
        _nonnegative_int(
            self.fencing_generation,
            "graph_activity_result.fencing_generation",
        )
        _require_contract_kind(
            self.activity_ref,
            HarnessContractKind.ACTIVITY,
            "graph_activity_result.activity_ref",
        )
        object.__setattr__(
            self,
            "evidence_ref",
            _checksum(self.evidence_ref, "graph_activity_result.evidence_ref"),
        )
        object.__setattr__(
            self,
            "payload_ref",
            _checksum(self.payload_ref, "graph_activity_result.payload_ref"),
        )
        object.__setattr__(self, "status", HarnessGraphActivityResultStatus(self.status))
        termination_confirmed = self.termination_confirmed
        if termination_confirmed is None:
            if self.status is HarnessGraphActivityResultStatus.SUCCEEDED:
                termination_confirmed = True
            elif self.status in {
                HarnessGraphActivityResultStatus.FAILED,
                HarnessGraphActivityResultStatus.TIMEOUT,
                HarnessGraphActivityResultStatus.CANCELLED,
            }:
                raise HarnessValidationError(
                    "non-success activity results require explicit termination confirmation",
                    code="graph_activity_termination_confirmation_missing",
                )
            else:
                termination_confirmed = False
        elif not isinstance(termination_confirmed, bool):
            raise HarnessValidationError(
                "graph activity result termination_confirmed must be boolean",
                code="invalid_graph_activity_termination",
            )
        if (
            self.status is HarnessGraphActivityResultStatus.SUCCEEDED
            and not termination_confirmed
        ):
            raise HarnessValidationError(
                "successful activity results require confirmed termination",
                code="invalid_graph_activity_termination",
            )
        object.__setattr__(self, "termination_confirmed", termination_confirmed)
        for field_name in _GRAPH_SCOPE_FIELDS:
            object.__setattr__(
                self,
                field_name,
                _optional_checksum(getattr(self, field_name), field_name),
            )
        lineage = self.result_lineage
        if lineage is not None:
            if not isinstance(lineage, HarnessGraphResultLineage):
                raise TypeError("result_lineage must be HarnessGraphResultLineage")
            expected_statuses = {
                HarnessGraphActivityResultStatus.SUCCEEDED: frozenset(
                    {HarnessGraphResultLineageStatus.SUCCEEDED}
                ),
                HarnessGraphActivityResultStatus.FAILED: frozenset(
                    {
                        HarnessGraphResultLineageStatus.FAILED,
                        HarnessGraphResultLineageStatus.HALTED,
                    }
                ),
                HarnessGraphActivityResultStatus.TIMEOUT: frozenset(
                    {
                        HarnessGraphResultLineageStatus.FAILED,
                        HarnessGraphResultLineageStatus.HALTED,
                    }
                ),
                HarnessGraphActivityResultStatus.CANCELLED: frozenset(
                    {
                        HarnessGraphResultLineageStatus.SKIPPED,
                        HarnessGraphResultLineageStatus.HALTED,
                    }
                ),
                HarnessGraphActivityResultStatus.INDETERMINATE: frozenset(
                    {HarnessGraphResultLineageStatus.HALTED}
                ),
            }
            if (
                lineage.node_instance_id != self.node_instance_id
                or lineage.attempt != self.attempt
                or lineage.status not in expected_statuses[self.status]
                or self.evidence_ref != lineage.lineage_checksum
                or self.payload_ref != lineage.envelope_checksum
                or lineage.tenant_scope_ref != self.tenant_scope_ref
                or lineage.identity_scope_ref != self.identity_scope_ref
                or lineage.subject_scope_ref != self.subject_scope_ref
            ):
                raise HarnessValidationError(
                    "graph activity result lineage conflicts with its attempt",
                    code="graph_result_lineage_activity_mismatch",
                )
        if self.schema_version != HARNESS_GRAPH_ACTIVITY_RESULT_SCHEMA:
            raise HarnessValidationError(
                "unsupported graph activity result schema",
                code="unsupported_graph_activity_result_schema",
            )
        object.__setattr__(
            self,
            "result_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    @classmethod
    def for_activity(
        cls,
        activity: HarnessGraphActivity,
        *,
        evidence_ref: str,
        payload_ref: str,
        status: HarnessGraphActivityResultStatus | str,
        termination_confirmed: bool | None = None,
        result_lineage: HarnessGraphResultLineage | None = None,
    ) -> HarnessGraphActivityResult:
        if not isinstance(activity, HarnessGraphActivity):
            raise TypeError("activity must be HarnessGraphActivity")
        return cls(
            activity_id=activity.activity_id,
            node_instance_id=activity.node_instance_id,
            attempt=activity.attempt,
            idempotency_key=activity.idempotency_key,
            fencing_generation=activity.fencing_generation,
            activity_ref=activity.activity_ref,
            evidence_ref=evidence_ref,
            payload_ref=payload_ref,
            status=status,
            termination_confirmed=termination_confirmed,
            tenant_scope_ref=activity.tenant_scope_ref,
            identity_scope_ref=activity.identity_scope_ref,
            subject_scope_ref=activity.subject_scope_ref,
            result_lineage=result_lineage,
        )

    def checksum_projection(self) -> dict[str, Any]:
        projection = {
            "schema_version": self.schema_version,
            "activity_id": self.activity_id,
            "node_instance_id": self.node_instance_id,
            "attempt": self.attempt,
            "idempotency_key": self.idempotency_key,
            "fencing_generation": self.fencing_generation,
            "activity_ref": self.activity_ref.to_dict(),
            "evidence_ref": self.evidence_ref,
            "payload_ref": self.payload_ref,
            "status": self.status.value,
            "termination_confirmed": self.termination_confirmed,
            "tenant_scope_ref": self.tenant_scope_ref,
            "identity_scope_ref": self.identity_scope_ref,
            "subject_scope_ref": self.subject_scope_ref,
        }
        if self.result_lineage is not None:
            projection["result_lineage"] = self.result_lineage.to_dict()
        return projection

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "result_checksum": self.result_checksum}


@dataclass(frozen=True, slots=True)
class HarnessGraphDecisionCommit:
    decision: HarnessGraphDecision
    sequence: int
    occurred_at: datetime
    activity_input_ref: str | None = None
    accepted_evidence_refs: tuple[str, ...] = ()
    side_effect_outcome_ref: str | None = None
    schema_version: str = HARNESS_GRAPH_COMMIT_SCHEMA
    commit_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.decision, HarnessGraphDecision):
            raise TypeError("decision must be HarnessGraphDecision")
        _positive_int(self.sequence, "graph_decision_commit.sequence")
        occurred_at = _datetime(self.occurred_at, "graph_decision_commit.occurred_at")
        activity_input_ref = _optional_checksum(
            self.activity_input_ref,
            "graph_decision_commit.activity_input_ref",
        )
        if (
            self.decision.decision_type is HarnessGraphDecisionType.DISPATCH_ACTIVITY
        ) != (activity_input_ref is not None):
            raise HarnessValidationError(
                "only activity dispatch decisions may carry an input reference",
                code="graph_decision_activity_input_mismatch",
            )
        accepted_evidence_refs = tuple(
            sorted(
                _checksum(item, "graph_decision_commit.accepted_evidence_refs")
                for item in self.accepted_evidence_refs
            )
        )
        if len(accepted_evidence_refs) != len(set(accepted_evidence_refs)):
            raise HarnessValidationError(
                "accepted decision evidence references must be unique",
                code="duplicate_graph_decision_evidence",
            )
        side_effect_outcome_ref = _optional_checksum(
            self.side_effect_outcome_ref,
            "graph_decision_commit.side_effect_outcome_ref",
        )
        if (
            side_effect_outcome_ref is not None
            and self.decision.decision_type
            not in {
                HarnessGraphDecisionType.COMPLETE_NODE,
                HarnessGraphDecisionType.COMPLETE_RUN,
            }
        ):
            raise HarnessValidationError(
                "only node or run completion may carry side-effect outcome evidence",
                code="graph_decision_side_effect_outcome_mismatch",
            )
        if (
            side_effect_outcome_ref is not None
            and side_effect_outcome_ref not in accepted_evidence_refs
        ):
            raise HarnessValidationError(
                "side-effect outcome must be an accepted decision evidence reference",
                code="graph_decision_side_effect_evidence_missing",
            )
        if self.schema_version != HARNESS_GRAPH_COMMIT_SCHEMA:
            raise HarnessValidationError(
                "unsupported graph commit schema",
                code="unsupported_graph_commit_schema",
            )
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "activity_input_ref", activity_input_ref)
        object.__setattr__(
            self,
            "accepted_evidence_refs",
            accepted_evidence_refs,
        )
        object.__setattr__(
            self,
            "side_effect_outcome_ref",
            side_effect_outcome_ref,
        )
        object.__setattr__(
            self,
            "commit_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "commit_kind": HarnessGraphCommitKind.DECISION.value,
            "decision": self.decision.to_dict(),
            "sequence": self.sequence,
            "occurred_at": format_datetime(self.occurred_at),
            "activity_input_ref": self.activity_input_ref,
            "accepted_evidence_refs": list(self.accepted_evidence_refs),
            "side_effect_outcome_ref": self.side_effect_outcome_ref,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "commit_checksum": self.commit_checksum}


@dataclass(frozen=True, slots=True)
class HarnessGraphActivityResultCommit:
    result: HarnessGraphActivityResult
    sequence: int
    occurred_at: datetime
    schema_version: str = HARNESS_GRAPH_COMMIT_SCHEMA
    commit_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.result, HarnessGraphActivityResult):
            raise TypeError("result must be HarnessGraphActivityResult")
        _positive_int(self.sequence, "graph_activity_result_commit.sequence")
        occurred_at = _datetime(
            self.occurred_at,
            "graph_activity_result_commit.occurred_at",
        )
        if self.schema_version != HARNESS_GRAPH_COMMIT_SCHEMA:
            raise HarnessValidationError(
                "unsupported graph commit schema",
                code="unsupported_graph_commit_schema",
            )
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(
            self,
            "commit_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "commit_kind": HarnessGraphCommitKind.ACTIVITY_RESULT.value,
            "result": self.result.to_dict(),
            "sequence": self.sequence,
            "occurred_at": format_datetime(self.occurred_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "commit_checksum": self.commit_checksum}


@dataclass(frozen=True, slots=True)
class HarnessGraphObservationCommit:
    observation: HarnessAcceptedGraphObservation
    sequence: int
    occurred_at: datetime
    schema_version: str = HARNESS_GRAPH_COMMIT_SCHEMA
    commit_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.observation, HarnessAcceptedGraphObservation):
            raise TypeError("observation must be HarnessAcceptedGraphObservation")
        _positive_int(self.sequence, "graph_observation_commit.sequence")
        if self.observation.event_sequence != self.sequence:
            raise HarnessValidationError(
                "graph observation sequence must match its durable commit",
                code="graph_observation_sequence_mismatch",
            )
        occurred_at = _datetime(
            self.occurred_at,
            "graph_observation_commit.occurred_at",
        )
        if self.schema_version != HARNESS_GRAPH_COMMIT_SCHEMA:
            raise HarnessValidationError(
                "unsupported graph commit schema",
                code="unsupported_graph_commit_schema",
            )
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(
            self,
            "commit_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "commit_kind": HarnessGraphCommitKind.OBSERVATION.value,
            "observation": self.observation.to_dict(),
            "sequence": self.sequence,
            "occurred_at": format_datetime(self.occurred_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "commit_checksum": self.commit_checksum}


@dataclass(frozen=True, slots=True)
class HarnessGraphProjectionCommit:
    commit_kind: HarnessGraphCommitKind | str
    cause_checksum: str
    previous_projection_checksum: str | None
    state: HarnessGraphState
    sequence: int
    occurred_at: datetime
    budget_reservations: Mapping[str, Any] = field(default_factory=dict)
    budget_consumptions: Mapping[str, Any] = field(default_factory=dict)
    activity: HarnessGraphActivity | None = None
    schema_version: str = HARNESS_GRAPH_COMMIT_SCHEMA
    commit_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        commit_kind = HarnessGraphCommitKind(self.commit_kind)
        if commit_kind not in {
            HarnessGraphCommitKind.INITIALIZE,
            HarnessGraphCommitKind.DECISION_PROJECTION,
            HarnessGraphCommitKind.ACTIVITY_RESULT_PROJECTION,
            HarnessGraphCommitKind.OBSERVATION_PROJECTION,
        }:
            raise HarnessValidationError(
                "projection commit kind is invalid",
                code="invalid_graph_projection_commit_kind",
            )
        cause_checksum = _checksum(
            self.cause_checksum,
            "graph_projection_commit.cause_checksum",
        )
        previous_checksum = _optional_checksum(
            self.previous_projection_checksum,
            "graph_projection_commit.previous_projection_checksum",
        )
        if not isinstance(self.state, HarnessGraphState):
            raise TypeError("state must be HarnessGraphState")
        _positive_int(self.sequence, "graph_projection_commit.sequence")
        if self.state.last_event_sequence != self.sequence:
            raise HarnessValidationError(
                "graph projection sequence must match state last_event_sequence",
                code="graph_projection_sequence_mismatch",
            )
        reservations = _counter_delta(self.budget_reservations, "budget_reservations")
        consumptions = _counter_delta(self.budget_consumptions, "budget_consumptions")
        if reservations != consumptions:
            raise HarnessValidationError(
                "graph budget reservations must equal consumptions",
                code="graph_budget_reservation_mismatch",
            )
        if self.activity is not None and not isinstance(
            self.activity,
            HarnessGraphActivity,
        ):
            raise TypeError("activity must be HarnessGraphActivity")
        if commit_kind is HarnessGraphCommitKind.INITIALIZE:
            if (
                self.sequence != 1
                or previous_checksum is not None
                or cause_checksum != self.state.graph_ref.checksum
            ):
                raise HarnessValidationError(
                    "graph initialization projection has an invalid causal shape",
                    code="graph_initial_projection_mismatch",
                )
            if reservations or consumptions or self.activity is not None:
                raise HarnessValidationError(
                    "graph initialization projection cannot reserve budget or activity",
                    code="graph_initial_projection_mismatch",
                )
        elif previous_checksum is None:
            raise HarnessValidationError(
                "non-initial graph projection requires a previous projection checksum",
                code="graph_projection_chain_mismatch",
            )
        if self.activity is not None:
            if commit_kind is not HarnessGraphCommitKind.DECISION_PROJECTION:
                raise HarnessValidationError(
                    "only a decision projection may create an activity",
                    code="graph_projection_activity_mismatch",
                )
            if self.activity.causal_decision_checksum != cause_checksum:
                raise HarnessValidationError(
                    "graph projection activity is not caused by its decision",
                    code="graph_projection_activity_mismatch",
                )
            matching = tuple(
                item
                for item in self.state.active_activities
                if item.activity_id == self.activity.activity_id
                and item.activity_ref == self.activity.activity_ref
                and item.node_instance_id == self.activity.node_instance_id
                and item.attempt == self.activity.attempt
                and item.idempotency_key == self.activity.idempotency_key
                and item.fencing_generation == self.activity.fencing_generation
                and item.dispatched_sequence
                == self.activity.causal_decision_sequence
            )
            if (
                self.activity.run_id != self.state.run_id
                or self.activity.graph_ref != self.state.graph_ref
                or self.sequence != self.activity.causal_decision_sequence + 1
                or len(matching) != 1
            ):
                raise HarnessValidationError(
                    "graph projection activity does not match its committed state",
                    code="graph_projection_activity_mismatch",
                )
        occurred_at = _datetime(
            self.occurred_at,
            "graph_projection_commit.occurred_at",
        )
        if self.schema_version != HARNESS_GRAPH_COMMIT_SCHEMA:
            raise HarnessValidationError(
                "unsupported graph commit schema",
                code="unsupported_graph_commit_schema",
            )
        object.__setattr__(self, "commit_kind", commit_kind)
        object.__setattr__(self, "cause_checksum", cause_checksum)
        object.__setattr__(self, "previous_projection_checksum", previous_checksum)
        object.__setattr__(self, "budget_reservations", reservations)
        object.__setattr__(self, "budget_consumptions", consumptions)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(
            self,
            "commit_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "commit_kind": self.commit_kind.value,
            "cause_checksum": self.cause_checksum,
            "previous_projection_checksum": self.previous_projection_checksum,
            "state": self.state.to_dict(),
            "sequence": self.sequence,
            "occurred_at": format_datetime(self.occurred_at),
            "budget_reservations": thaw_json(self.budget_reservations),
            "budget_consumptions": thaw_json(self.budget_consumptions),
            "activity": None if self.activity is None else self.activity.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "commit_checksum": self.commit_checksum}


@dataclass(frozen=True, slots=True)
class HarnessGraphRecovery:
    run_id: str
    graph: NormalizedHarnessGraph | None
    run_spec_checksum: str | None
    state: HarnessGraphState | None
    expected_last_sequence: int
    decision_commits: tuple[HarnessGraphDecisionCommit, ...] = ()
    projection_commits: tuple[HarnessGraphProjectionCommit, ...] = ()
    activity_result_commits: tuple[HarnessGraphActivityResultCommit, ...] = ()
    observation_commits: tuple[HarnessGraphObservationCommit, ...] = ()
    phase_transition_records: tuple[GraphPhaseTransitionRecord, ...] = ()
    activities: tuple[HarnessGraphActivity, ...] = ()
    dispatched_activity_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", required_text(self.run_id, "graph_recovery.run_id"))
        if self.graph is not None and not isinstance(self.graph, NormalizedHarnessGraph):
            raise TypeError("graph must be NormalizedHarnessGraph")
        run_spec_checksum = _optional_checksum(
            self.run_spec_checksum,
            "graph_recovery.run_spec_checksum",
        )
        if self.state is not None and not isinstance(self.state, HarnessGraphState):
            raise TypeError("state must be HarnessGraphState")
        _nonnegative_int(
            self.expected_last_sequence,
            "graph_recovery.expected_last_sequence",
        )
        object.__setattr__(self, "run_spec_checksum", run_spec_checksum)
        decisions = tuple(self.decision_commits)
        projections = tuple(self.projection_commits)
        results = tuple(self.activity_result_commits)
        observations = tuple(self.observation_commits)
        phase_transitions = tuple(self.phase_transition_records)
        activities = tuple(self.activities)
        dispatched_activity_ids = frozenset(self.dispatched_activity_ids)
        if not all(isinstance(item, HarnessGraphDecisionCommit) for item in decisions):
            raise TypeError("decision_commits must contain HarnessGraphDecisionCommit values")
        if not all(isinstance(item, HarnessGraphProjectionCommit) for item in projections):
            raise TypeError("projection_commits must contain HarnessGraphProjectionCommit values")
        if not all(
            isinstance(item, HarnessGraphActivityResultCommit) for item in results
        ):
            raise TypeError(
                "activity_result_commits must contain HarnessGraphActivityResultCommit values"
            )
        if not all(
            isinstance(item, HarnessGraphObservationCommit) for item in observations
        ):
            raise TypeError(
                "observation_commits must contain HarnessGraphObservationCommit values"
            )
        if not all(
            isinstance(item, GraphPhaseTransitionRecord)
            for item in phase_transitions
        ):
            raise TypeError(
                "phase_transition_records must contain "
                "GraphPhaseTransitionRecord values"
            )
        if not all(isinstance(item, HarnessGraphActivity) for item in activities):
            raise TypeError("activities must contain HarnessGraphActivity values")
        decisions = tuple(sorted(decisions, key=lambda item: item.sequence))
        projections = tuple(sorted(projections, key=lambda item: item.sequence))
        results = tuple(sorted(results, key=lambda item: item.sequence))
        observations = tuple(sorted(observations, key=lambda item: item.sequence))
        phase_transitions = tuple(
            sorted(phase_transitions, key=lambda item: item.event_sequence)
        )
        ordered_sequences = tuple(
            sorted(
                (
                    *(item.sequence for item in decisions),
                    *(item.sequence for item in projections),
                    *(item.sequence for item in results),
                    *(item.sequence for item in observations),
                    *(item.event_sequence for item in phase_transitions),
                )
            )
        )
        if len(ordered_sequences) != len(set(ordered_sequences)):
            raise EventStoreCorruptionError(
                "graph recovery contains duplicate stream sequences"
            )
        if self.expected_last_sequence == 0:
            if (
                ordered_sequences
                or self.graph is not None
                or self.run_spec_checksum is not None
                or self.state is not None
                or phase_transitions
                or activities
                or dispatched_activity_ids
            ):
                raise EventStoreCorruptionError(
                    "empty graph recovery contains durable history"
                )
            object.__setattr__(self, "decision_commits", decisions)
            object.__setattr__(self, "projection_commits", projections)
            object.__setattr__(self, "activity_result_commits", results)
            object.__setattr__(self, "observation_commits", observations)
            object.__setattr__(
                self,
                "phase_transition_records",
                phase_transitions,
            )
            object.__setattr__(self, "activities", activities)
            object.__setattr__(self, "dispatched_activity_ids", dispatched_activity_ids)
            return
        if (
            not ordered_sequences
            or ordered_sequences[0] != 1
            or ordered_sequences[-1] > self.expected_last_sequence
        ):
            raise EventStoreCorruptionError(
                "graph recovery stream sequences are outside canonical order"
            )
        if self.graph is None or run_spec_checksum is None or self.state is None:
            raise EventStoreCorruptionError(
                "non-empty graph recovery is missing its pinned identity"
            )
        graph_ref = graph_reference(self.graph)
        if self.state.run_id != self.run_id:
            raise EventStoreCorruptionError(
                "graph recovery state belongs to another run"
            )
        if self.state.graph_ref != graph_ref:
            raise EventStoreCorruptionError(
                "graph recovery state does not use its pinned graph reference"
            )
        if self.state.metadata.get("run_spec_checksum") != run_spec_checksum:
            raise EventStoreCorruptionError(
                "graph recovery state does not use its pinned run specification"
            )
        initialization = tuple(
            item
            for item in projections
            if item.commit_kind is HarnessGraphCommitKind.INITIALIZE
        )
        if (
            len(initialization) != 1
            or initialization[0].sequence != 1
            or initialization[0].cause_checksum != self.graph.checksum
            or initialization[0].previous_projection_checksum is not None
        ):
            raise EventStoreCorruptionError(
                "graph recovery is missing a valid initial projection"
            )
        decision_by_checksum = {
            item.decision.decision_checksum: item for item in decisions
        }
        if len(decision_by_checksum) != len(decisions):
            raise EventStoreCorruptionError(
                "graph recovery contains duplicate decision identities"
            )
        result_by_checksum = {
            item.result.result_checksum: item for item in results
        }
        if len(result_by_checksum) != len(results):
            raise EventStoreCorruptionError(
                "graph recovery contains duplicate activity result identities"
            )
        observation_by_checksum = {
            item.observation.observation_checksum: item for item in observations
        }
        if len(observation_by_checksum) != len(observations):
            raise EventStoreCorruptionError(
                "graph recovery contains duplicate observation identities"
            )
        cause_checksums = {
            *decision_by_checksum,
            *result_by_checksum,
            *observation_by_checksum,
        }
        if len(cause_checksums) != (
            len(decision_by_checksum)
            + len(result_by_checksum)
            + len(observation_by_checksum)
        ):
            raise EventStoreCorruptionError(
                "graph recovery contains colliding cause identities"
            )
        if self.graph.checksum in cause_checksums:
            raise EventStoreCorruptionError(
                "graph recovery cause identity collides with graph initialization"
            )
        for commit in decisions:
            if (
                commit.decision.run_id != self.run_id
                or commit.decision.graph_ref != graph_ref
            ):
                raise EventStoreCorruptionError(
                    "graph decision commit is outside the recovered run"
                )
        projection_by_cause: dict[str, HarnessGraphProjectionCommit] = {}
        for index, projection in enumerate(projections):
            if projection.state.run_id != self.run_id or projection.state.graph_ref != graph_ref:
                raise EventStoreCorruptionError(
                    "graph projection state is outside the recovered run"
                )
            if projection.cause_checksum in projection_by_cause:
                raise EventStoreCorruptionError(
                    "graph recovery contains duplicate projections for one cause"
                )
            projection_by_cause[projection.cause_checksum] = projection
            if projection.commit_kind is HarnessGraphCommitKind.INITIALIZE:
                continue
            if index == 0:
                raise EventStoreCorruptionError(
                    "graph projection chain starts before initialization"
                )
            previous = projections[index - 1]
            if projection.previous_projection_checksum != previous.state.projection_checksum:
                raise EventStoreCorruptionError(
                    "graph projection chain has a checksum gap"
                )
            if projection.commit_kind is HarnessGraphCommitKind.DECISION_PROJECTION:
                cause = decision_by_checksum.get(projection.cause_checksum)
                if cause is None or projection.sequence != cause.sequence + 1:
                    raise EventStoreCorruptionError(
                        "graph decision projection is not adjacent to its cause"
                    )
            elif (
                projection.commit_kind
                is HarnessGraphCommitKind.ACTIVITY_RESULT_PROJECTION
            ):
                cause = result_by_checksum.get(projection.cause_checksum)
                if cause is None or projection.sequence != cause.sequence + 1:
                    raise EventStoreCorruptionError(
                        "graph activity result projection is not adjacent to its cause"
                    )
            else:
                cause = observation_by_checksum.get(projection.cause_checksum)
                if cause is None or projection.sequence != cause.sequence + 1:
                    raise EventStoreCorruptionError(
                        "graph observation projection is not adjacent to its cause"
                    )
        latest_projection = projections[-1]
        if (
            self.state.last_event_sequence != latest_projection.sequence
            or self.state.projection_checksum != latest_projection.state.projection_checksum
        ):
            raise EventStoreCorruptionError(
                "graph recovery state does not match its latest projection"
            )
        unprojected = tuple(
            sorted(
                (
                    *(item for item in decisions if item.decision.decision_checksum not in projection_by_cause),
                    *(item for item in results if item.result.result_checksum not in projection_by_cause),
                    *(
                        item
                        for item in observations
                        if item.observation.observation_checksum
                        not in projection_by_cause
                    ),
                ),
                key=lambda item: item.sequence,
            )
        )
        if len(unprojected) > 1 or (
            unprojected and unprojected[0].sequence != self.expected_last_sequence
        ):
            raise EventStoreCorruptionError(
                "graph recovery contains an invalid unprojected causal record"
            )
        activity_ids = {item.activity_id for item in activities}
        if len(activity_ids) != len(activities):
            raise EventStoreCorruptionError(
                "graph recovery contains duplicate activity identities"
            )
        if not dispatched_activity_ids.issubset(activity_ids):
            raise EventStoreCorruptionError(
                "graph recovery dispatch markers reference unknown activities"
            )
        if not {
            item.result.activity_id for item in results
        }.issubset(activity_ids):
            raise EventStoreCorruptionError(
                "graph recovery results reference unknown activities"
            )
        result_activity_ids: set[str] = set()
        projection_activity_ids: set[str] = set()
        activity_by_id = {item.activity_id: item for item in activities}
        for commit in results:
            activity_id = commit.result.activity_id
            if activity_id in result_activity_ids:
                raise EventStoreCorruptionError(
                    "graph recovery contains duplicate results for one activity"
                )
            result_activity_ids.add(activity_id)
            validate_graph_activity_result(activity_by_id[activity_id], commit.result)
        for activity in activities:
            if activity.run_id != self.run_id or activity.graph_ref != graph_ref:
                raise EventStoreCorruptionError(
                    "graph activity is outside the recovered run"
                )
            cause = decision_by_checksum.get(activity.causal_decision_checksum)
            projection = projection_by_cause.get(activity.causal_decision_checksum)
            if cause is None or projection is None or projection.activity != activity:
                raise EventStoreCorruptionError(
                    "graph activity is not bound to its dispatch decision"
                )
            _validate_graph_activity_binding(activity, cause, projection.state)
        for projection in projections:
            if projection.activity is None:
                continue
            activity_id = projection.activity.activity_id
            if activity_id in projection_activity_ids:
                raise EventStoreCorruptionError(
                    "graph recovery contains duplicate activity projections"
                )
            projection_activity_ids.add(activity_id)
            if activity_by_id.get(activity_id) != projection.activity:
                raise EventStoreCorruptionError(
                    "graph activity descriptor differs from its projection"
                )
        if projection_activity_ids != activity_ids:
            raise EventStoreCorruptionError(
                "graph activity history is not represented by projections"
            )
        projected_result_activity_ids = {
            item.result.activity_id
            for item in results
            if item.result.result_checksum in projection_by_cause
            and item.result.termination_confirmed
        }
        active_activity_ids = {
            item.activity_id for item in self.state.active_activities
        }
        if not active_activity_ids.issubset(
            activity_ids
        ) or active_activity_ids.intersection(projected_result_activity_ids):
            raise EventStoreCorruptionError(
                "graph active activity projection is inconsistent with history"
            )
        for active in self.state.active_activities:
            activity = activity_by_id[active.activity_id]
            if (
                active.activity_ref != activity.activity_ref
                or active.node_instance_id != activity.node_instance_id
                or active.attempt != activity.attempt
                or active.idempotency_key != activity.idempotency_key
                or active.fencing_generation != activity.fencing_generation
                or active.dispatched_sequence != activity.causal_decision_sequence
            ):
                raise EventStoreCorruptionError(
                    "graph active activity descriptor differs from durable history"
                )
        object.__setattr__(self, "decision_commits", decisions)
        object.__setattr__(self, "projection_commits", projections)
        object.__setattr__(self, "activity_result_commits", results)
        object.__setattr__(self, "observation_commits", observations)
        object.__setattr__(
            self,
            "phase_transition_records",
            phase_transitions,
        )
        object.__setattr__(self, "activities", activities)
        object.__setattr__(
            self,
            "dispatched_activity_ids",
            dispatched_activity_ids,
        )

    @property
    def projected_cause_checksums(self) -> frozenset[str]:
        return frozenset(item.cause_checksum for item in self.projection_commits)

    @property
    def pending_decisions(self) -> tuple[HarnessGraphDecisionCommit, ...]:
        projected = self.projected_cause_checksums
        return tuple(
            item
            for item in self.decision_commits
            if item.decision.decision_checksum not in projected
        )

    @property
    def pending_activity_results(self) -> tuple[HarnessGraphActivityResultCommit, ...]:
        projected = self.projected_cause_checksums
        return tuple(
            item
            for item in self.activity_result_commits
            if item.result.result_checksum not in projected
        )

    @property
    def pending_observations(self) -> tuple[HarnessGraphObservationCommit, ...]:
        projected = self.projected_cause_checksums
        return tuple(
            item
            for item in self.observation_commits
            if item.observation.observation_checksum not in projected
        )


@runtime_checkable
class HarnessGraphTransitionPort(Protocol):
    """Durable CAS boundary for graph decisions and their state projections."""

    def initialize_graph(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        *,
        run_spec_checksum: str,
        occurred_at: datetime,
        expected_last_sequence: int,
    ) -> HarnessGraphProjectionCommit: ...

    def commit_graph_decision(
        self,
        decision: HarnessGraphDecision,
        *,
        occurred_at: datetime,
        expected_last_sequence: int,
        activity_input_ref: str | None = None,
        accepted_evidence_refs: tuple[str, ...] = (),
        side_effect_outcome_ref: str | None = None,
    ) -> HarnessGraphDecisionCommit: ...

    def commit_graph_projection(
        self,
        commit: HarnessGraphProjectionCommit,
        *,
        expected_last_sequence: int,
    ) -> HarnessGraphProjectionCommit: ...

    def commit_graph_activity_result(
        self,
        result: HarnessGraphActivityResult,
        *,
        occurred_at: datetime,
        expected_last_sequence: int,
    ) -> HarnessGraphActivityResultCommit: ...

    def commit_graph_observation(
        self,
        observation: HarnessAcceptedGraphObservation,
        *,
        occurred_at: datetime,
        expected_last_sequence: int,
    ) -> HarnessGraphObservationCommit: ...

    def recover_graph(self, run_id: str) -> HarnessGraphRecovery: ...

    def activity_for(self, activity_id: str) -> HarnessGraphActivity | None: ...

    def mark_activity_dispatched(self, activity_id: str) -> None: ...


@dataclass(slots=True)
class _InMemoryGraphJournal:
    graph: NormalizedHarnessGraph
    run_spec_checksum: str
    state: HarnessGraphState
    last_sequence: int
    decision_commits: list[HarnessGraphDecisionCommit]
    projection_commits: list[HarnessGraphProjectionCommit]
    activity_result_commits: list[HarnessGraphActivityResultCommit]
    observation_commits: list[HarnessGraphObservationCommit]
    activities: dict[str, HarnessGraphActivity]
    dispatched_activity_ids: set[str]


class InMemoryHarnessGraphTransitionPort:
    """Explicit test-only graph journal with the production CAS semantics."""

    def __init__(self) -> None:
        self._journals: dict[str, _InMemoryGraphJournal] = {}
        self._lock = RLock()

    def initialize_graph(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        *,
        run_spec_checksum: str,
        occurred_at: datetime,
        expected_last_sequence: int,
    ) -> HarnessGraphProjectionCommit:
        if not isinstance(graph, NormalizedHarnessGraph):
            raise TypeError("graph must be NormalizedHarnessGraph")
        if not isinstance(state, HarnessGraphState):
            raise TypeError("state must be HarnessGraphState")
        run_spec_ref = _checksum(run_spec_checksum, "run_spec_checksum")
        with self._lock:
            existing = self._journals.get(state.run_id)
            if existing is not None:
                if (
                    existing.graph != graph
                    or existing.run_spec_checksum != run_spec_ref
                    or existing.state.projection_checksum != state.projection_checksum
                ):
                    raise EventReplayMismatchError(
                        sequence=existing.last_sequence,
                        reason="graph initialization conflicts with committed run identity",
                    )
                return existing.projection_commits[0]
            if expected_last_sequence != 0:
                raise EventReplayMismatchError(
                    sequence=expected_last_sequence,
                    reason="graph initialization requires an empty run stream",
                )
            if state.last_event_sequence != 1:
                raise HarnessValidationError(
                    "initial graph state must project the graph-created sequence",
                    code="graph_initial_sequence_mismatch",
                )
            commit = HarnessGraphProjectionCommit(
                HarnessGraphCommitKind.INITIALIZE,
                graph.checksum,
                None,
                state,
                1,
                occurred_at,
            )
            self._journals[state.run_id] = _InMemoryGraphJournal(
                graph=graph,
                run_spec_checksum=run_spec_ref,
                state=state,
                last_sequence=1,
                decision_commits=[],
                projection_commits=[commit],
                activity_result_commits=[],
                observation_commits=[],
                activities={},
                dispatched_activity_ids=set(),
            )
            return commit

    def commit_graph_decision(
        self,
        decision: HarnessGraphDecision,
        *,
        occurred_at: datetime,
        expected_last_sequence: int,
        activity_input_ref: str | None = None,
        accepted_evidence_refs: tuple[str, ...] = (),
        side_effect_outcome_ref: str | None = None,
    ) -> HarnessGraphDecisionCommit:
        if not isinstance(decision, HarnessGraphDecision):
            raise TypeError("decision must be HarnessGraphDecision")
        with self._lock:
            journal = self._require_journal(decision.run_id)
            _validate_graph_decision_storage_identity(
                journal.graph,
                journal.state,
                decision,
            )
            existing = next(
                (
                    item
                    for item in journal.decision_commits
                    if item.decision.decision_checksum == decision.decision_checksum
                ),
                None,
            )
            if existing is not None:
                normalized_input_ref = _optional_checksum(
                    activity_input_ref,
                    "activity_input_ref",
                )
                normalized_evidence = tuple(
                    sorted(
                        _checksum(item, "accepted_evidence_refs")
                        for item in accepted_evidence_refs
                    )
                )
                normalized_outcome_ref = _optional_checksum(
                    side_effect_outcome_ref,
                    "side_effect_outcome_ref",
                )
                if (
                    existing.decision != decision
                    or existing.activity_input_ref != normalized_input_ref
                    or existing.accepted_evidence_refs != normalized_evidence
                    or existing.side_effect_outcome_ref != normalized_outcome_ref
                ):
                    raise EventStoreCorruptionError(
                        "graph decision checksum resolves conflicting content"
                    )
                return existing
            self._require_stream_head(journal, expected_last_sequence)
            if journal.state.projection_checksum != decision.input_projection_checksum:
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph decision attempted from a stale projection",
                )
            recovery = self._recovery(decision.run_id, journal)
            if (
                recovery.pending_decisions
                or recovery.pending_activity_results
                or recovery.pending_observations
            ):
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph stream has a committed cause awaiting projection",
                )
            commit = HarnessGraphDecisionCommit(
                decision,
                journal.last_sequence + 1,
                occurred_at,
                activity_input_ref=activity_input_ref,
                accepted_evidence_refs=accepted_evidence_refs,
                side_effect_outcome_ref=side_effect_outcome_ref,
            )
            journal.decision_commits.append(commit)
            journal.last_sequence = commit.sequence
            return commit

    def commit_graph_projection(
        self,
        commit: HarnessGraphProjectionCommit,
        *,
        expected_last_sequence: int,
    ) -> HarnessGraphProjectionCommit:
        if not isinstance(commit, HarnessGraphProjectionCommit):
            raise TypeError("commit must be HarnessGraphProjectionCommit")
        with self._lock:
            journal = self._require_journal(commit.state.run_id)
            existing = next(
                (
                    item
                    for item in journal.projection_commits
                    if item.cause_checksum == commit.cause_checksum
                ),
                None,
            )
            if existing is not None:
                if existing != commit:
                    raise EventStoreCorruptionError(
                        "graph projection cause resolves conflicting content"
                    )
                return existing
            self._require_stream_head(journal, expected_last_sequence)
            if commit.sequence != journal.last_sequence + 1:
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph projection sequence is not contiguous",
                )
            if (
                commit.previous_projection_checksum
                != journal.state.projection_checksum
            ):
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph projection attempted from a stale state",
                )
            valid_causes = {
                *(item.decision.decision_checksum for item in journal.decision_commits),
                *(item.result.result_checksum for item in journal.activity_result_commits),
                *(
                    item.observation.observation_checksum
                    for item in journal.observation_commits
                ),
            }
            if commit.cause_checksum not in valid_causes:
                raise EventStoreCorruptionError(
                    "graph projection does not reference a committed cause"
                )
            decision_commit = next(
                (
                    item
                    for item in journal.decision_commits
                    if item.decision.decision_checksum == commit.cause_checksum
                ),
                None,
            )
            result_commit = next(
                (
                    item
                    for item in journal.activity_result_commits
                    if item.result.result_checksum == commit.cause_checksum
                ),
                None,
            )
            observation_commit = next(
                (
                    item
                    for item in journal.observation_commits
                    if item.observation.observation_checksum
                    == commit.cause_checksum
                ),
                None,
            )
            if (
                commit.commit_kind is HarnessGraphCommitKind.DECISION_PROJECTION
                and decision_commit is None
            ) or (
                commit.commit_kind is HarnessGraphCommitKind.ACTIVITY_RESULT_PROJECTION
                and result_commit is None
            ) or (
                commit.commit_kind is HarnessGraphCommitKind.OBSERVATION_PROJECTION
                and observation_commit is None
            ):
                raise EventStoreCorruptionError(
                    "graph projection kind does not match its committed cause"
                )
            if commit.commit_kind is HarnessGraphCommitKind.DECISION_PROJECTION:
                if commit.sequence != decision_commit.sequence + 1:
                    raise EventStoreCorruptionError(
                        "graph decision projection is not adjacent to its cause"
                    )
            elif (
                commit.commit_kind
                is HarnessGraphCommitKind.ACTIVITY_RESULT_PROJECTION
                and commit.sequence != result_commit.sequence + 1
            ):
                raise EventStoreCorruptionError(
                    "graph activity result projection is not adjacent to its cause"
                )
            elif (
                commit.commit_kind is HarnessGraphCommitKind.OBSERVATION_PROJECTION
                and commit.sequence != observation_commit.sequence + 1
            ):
                raise EventStoreCorruptionError(
                    "graph observation projection is not adjacent to its cause"
                )
            if commit.activity is not None:
                prior = journal.activities.get(commit.activity.activity_id)
                if prior is not None and prior != commit.activity:
                    raise EventStoreCorruptionError(
                        "graph activity identity resolves conflicting content"
                    )
                if decision_commit is None or (
                    decision_commit.activity_input_ref != commit.activity.input_ref
                ):
                    raise EventStoreCorruptionError(
                        "graph activity input does not match its decision commit"
                    )
                _validate_graph_activity_binding(
                    commit.activity,
                    decision_commit,
                    commit.state,
                )
                journal.activities[commit.activity.activity_id] = commit.activity
            journal.projection_commits.append(commit)
            journal.state = commit.state
            journal.last_sequence = commit.sequence
            return commit

    def commit_graph_activity_result(
        self,
        result: HarnessGraphActivityResult,
        *,
        occurred_at: datetime,
        expected_last_sequence: int,
    ) -> HarnessGraphActivityResultCommit:
        if not isinstance(result, HarnessGraphActivityResult):
            raise TypeError("result must be HarnessGraphActivityResult")
        with self._lock:
            journal = self._journal_for_activity(result.activity_id)
            existing = next(
                (
                    item
                    for item in journal.activity_result_commits
                    if item.result.activity_id == result.activity_id
                ),
                None,
            )
            if existing is not None:
                if existing.result != result:
                    raise EventReplayMismatchError(
                        sequence=existing.sequence,
                        reason="graph activity produced a conflicting duplicate result",
                    )
                return existing
            self._require_stream_head(journal, expected_last_sequence)
            activity = journal.activities[result.activity_id]
            validate_graph_activity_result(activity, result)
            if not any(
                item.activity_id == activity.activity_id
                for item in journal.state.active_activities
            ):
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph activity result is stale or already projected",
                )
            recovery = self._recovery(activity.run_id, journal)
            if (
                recovery.pending_decisions
                or recovery.pending_activity_results
                or recovery.pending_observations
            ):
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph stream has a committed cause awaiting projection",
                )
            commit = HarnessGraphActivityResultCommit(
                result,
                journal.last_sequence + 1,
                occurred_at,
            )
            journal.activity_result_commits.append(commit)
            journal.last_sequence = commit.sequence
            return commit

    def commit_graph_observation(
        self,
        observation: HarnessAcceptedGraphObservation,
        *,
        occurred_at: datetime,
        expected_last_sequence: int,
    ) -> HarnessGraphObservationCommit:
        if not isinstance(observation, HarnessAcceptedGraphObservation):
            raise TypeError("observation must be HarnessAcceptedGraphObservation")
        with self._lock:
            journal = (
                self._require_journal(observation.node_instance_id)
                if observation.observation_type
                is HarnessGraphObservationType.RUN_OPERATION
                else self._journal_for_node_instance(observation.node_instance_id)
            )
            existing = next(
                (
                    item
                    for item in journal.observation_commits
                    if item.observation.observation_checksum
                    == observation.observation_checksum
                ),
                None,
            )
            if existing is not None:
                if existing.observation != observation:
                    raise EventStoreCorruptionError(
                        "graph observation checksum resolves conflicting content"
                    )
                return existing
            self._require_stream_head(journal, expected_last_sequence)
            if observation.event_sequence != journal.last_sequence + 1:
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph observation sequence is not contiguous",
                )
            if observation.observation_type is not HarnessGraphObservationType.RUN_OPERATION:
                node = next(
                    (
                        item
                        for item in journal.state.node_instances
                        if item.instance_id == observation.node_instance_id
                    ),
                    None,
                )
                if (
                    node is None
                    or node.identity.node_id != observation.node_id
                    or node.attempt != observation.attempt
                ):
                    raise EventReplayMismatchError(
                        sequence=journal.last_sequence,
                        reason="graph observation does not match the current node attempt",
                    )
            recovery = self._recovery(journal.state.run_id, journal)
            if (
                recovery.pending_decisions
                or recovery.pending_activity_results
                or recovery.pending_observations
            ):
                raise EventReplayMismatchError(
                    sequence=journal.last_sequence,
                    reason="graph stream has a committed cause awaiting projection",
                )
            commit = HarnessGraphObservationCommit(
                observation,
                journal.last_sequence + 1,
                occurred_at,
            )
            journal.observation_commits.append(commit)
            journal.last_sequence = commit.sequence
            return commit

    def recover_graph(self, run_id: str) -> HarnessGraphRecovery:
        normalized_run_id = required_text(run_id, "run_id")
        with self._lock:
            journal = self._journals.get(normalized_run_id)
            if journal is None:
                return HarnessGraphRecovery(normalized_run_id, None, None, None, 0)
            return self._recovery(normalized_run_id, journal)

    def activity_for(self, activity_id: str) -> HarnessGraphActivity | None:
        normalized = required_text(activity_id, "activity_id")
        with self._lock:
            for journal in self._journals.values():
                activity = journal.activities.get(normalized)
                if activity is not None:
                    return activity
        return None

    def mark_activity_dispatched(self, activity_id: str) -> None:
        normalized = required_text(activity_id, "activity_id")
        with self._lock:
            journal = self._journal_for_activity(normalized)
            journal.dispatched_activity_ids.add(normalized)

    def _journal_for_activity(self, activity_id: str) -> _InMemoryGraphJournal:
        matches = tuple(
            journal
            for journal in self._journals.values()
            if activity_id in journal.activities
        )
        if len(matches) != 1:
            raise HarnessValidationError(
                "graph activity identity is unknown or ambiguous",
                code="graph_activity_identity_mismatch",
            )
        return matches[0]

    def _journal_for_node_instance(
        self,
        node_instance_id: str,
    ) -> _InMemoryGraphJournal:
        matches = tuple(
            journal
            for journal in self._journals.values()
            if any(
                item.instance_id == node_instance_id
                for item in journal.state.node_instances
            )
        )
        if len(matches) != 1:
            raise HarnessValidationError(
                "graph node instance identity is unknown or ambiguous",
                code="graph_observation_node_identity_mismatch",
            )
        return matches[0]

    def _require_journal(self, run_id: str) -> _InMemoryGraphJournal:
        journal = self._journals.get(run_id)
        if journal is None:
            raise EventReplayMismatchError(
                sequence=0,
                reason="graph run has not been durably initialized",
            )
        return journal

    @staticmethod
    def _require_stream_head(
        journal: _InMemoryGraphJournal,
        expected_last_sequence: int,
    ) -> None:
        if journal.last_sequence != expected_last_sequence:
            raise EventReplayMismatchError(
                sequence=journal.last_sequence,
                reason="graph commit attempted from a stale stream sequence",
            )

    @staticmethod
    def _recovery(
        run_id: str,
        journal: _InMemoryGraphJournal,
    ) -> HarnessGraphRecovery:
        return HarnessGraphRecovery(
            run_id=run_id,
            graph=journal.graph,
            run_spec_checksum=journal.run_spec_checksum,
            state=journal.state,
            expected_last_sequence=journal.last_sequence,
            decision_commits=tuple(journal.decision_commits),
            projection_commits=tuple(journal.projection_commits),
            activity_result_commits=tuple(journal.activity_result_commits),
            observation_commits=tuple(journal.observation_commits),
            activities=tuple(
                sorted(journal.activities.values(), key=lambda item: item.activity_id)
            ),
            dispatched_activity_ids=frozenset(journal.dispatched_activity_ids),
        )


def graph_reference(graph: NormalizedHarnessGraph) -> HarnessGraphReference:
    return HarnessGraphReference.from_graph(graph)


def initial_graph_state(
    run_spec: HarnessRunSpec,
    graph: NormalizedHarnessGraph,
    policy: HarnessGraphPreflightPolicy,
    *,
    run_spec_checksum: str,
    event_sequence: int = 1,
    runtime_scope_metadata: Mapping[str, Any] | None = None,
) -> HarnessGraphState:
    if not isinstance(run_spec, HarnessRunSpec):
        raise TypeError("run_spec must be HarnessRunSpec")
    if not isinstance(policy, HarnessGraphPreflightPolicy):
        raise TypeError("policy must be HarnessGraphPreflightPolicy")
    run_spec_ref = _checksum(run_spec_checksum, "run_spec_checksum")
    _positive_int(event_sequence, "event_sequence")
    retry_limit = run_spec.budget.max_retries_per_step * policy.max_node_activations
    counters = HarnessGraphBudgetState(
        (
            HarnessBudgetCounterState("max_active_nodes", policy.max_active_nodes),
            HarnessBudgetCounterState("max_parallelism", policy.max_parallelism),
            HarnessBudgetCounterState(
                "node_activations",
                policy.max_node_activations,
            ),
            HarnessBudgetCounterState(
                "compensations",
                policy.max_compensations,
            ),
            HarnessBudgetCounterState("replans", run_spec.budget.max_replans),
            HarnessBudgetCounterState("retries", retry_limit),
            HarnessBudgetCounterState("turns", run_spec.budget.max_turns),
            HarnessBudgetCounterState(
                "worker_calls",
                run_spec.budget.max_worker_calls,
            ),
        )
    )
    metadata: dict[str, Any] = {
        "run_spec_checksum": run_spec_ref,
        "graph_ref": graph.identity_ref.exact_ref,
        "graph_runtime_version": HARNESS_GRAPH_RUNTIME_VERSION,
    }
    runtime_scopes = {} if runtime_scope_metadata is None else runtime_scope_metadata
    if not isinstance(runtime_scopes, Mapping):
        raise TypeError("runtime_scope_metadata must be a mapping")
    unknown_scope_fields = set(runtime_scopes).difference(_GRAPH_SCOPE_FIELDS)
    if unknown_scope_fields:
        raise HarnessValidationError(
            "graph runtime scope metadata contains unsupported fields",
            code="graph_runtime_scope_mismatch",
            details={"fields": sorted(unknown_scope_fields)},
        )
    for field_name in _GRAPH_SCOPE_FIELDS:
        declared = run_spec.metadata.get(field_name)
        runtime_value = runtime_scopes.get(field_name)
        declared_ref = (
            None
            if declared is None
            else _checksum(declared, f"run_spec.metadata.{field_name}")
        )
        runtime_ref = (
            None
            if runtime_value is None
            else _checksum(runtime_value, f"runtime_scope_metadata.{field_name}")
        )
        if declared_ref is not None and runtime_ref is not None and declared_ref != runtime_ref:
            raise HarnessValidationError(
                "graph runtime scope conflicts with the declared run scope",
                code="graph_runtime_scope_mismatch",
                details={"field": field_name},
            )
        resolved = runtime_ref if runtime_ref is not None else declared_ref
        if resolved is not None:
            metadata[field_name] = resolved
    return HarnessGraphState(
        run_id=run_spec.run_id,
        graph_ref=graph_reference(graph),
        lifecycle="created",
        budgets=counters,
        last_event_sequence=event_sequence,
        metadata=metadata,
        schema_version=GRAPH_ONLY_HARNESS_GRAPH_STATE_SCHEMA,
    )


def validate_graph_activity_result(
    activity: HarnessGraphActivity,
    result: HarnessGraphActivityResult,
) -> None:
    if not isinstance(activity, HarnessGraphActivity):
        raise TypeError("activity must be HarnessGraphActivity")
    if not isinstance(result, HarnessGraphActivityResult):
        raise TypeError("result must be HarnessGraphActivityResult")
    mismatches = tuple(
        field_name
        for field_name, expected, actual in (
            ("activity_id", activity.activity_id, result.activity_id),
            ("node_instance_id", activity.node_instance_id, result.node_instance_id),
            ("attempt", activity.attempt, result.attempt),
            ("idempotency_key", activity.idempotency_key, result.idempotency_key),
            (
                "fencing_generation",
                activity.fencing_generation,
                result.fencing_generation,
            ),
            ("activity_ref", activity.activity_ref, result.activity_ref),
            ("tenant_scope_ref", activity.tenant_scope_ref, result.tenant_scope_ref),
            (
                "identity_scope_ref",
                activity.identity_scope_ref,
                result.identity_scope_ref,
            ),
            (
                "subject_scope_ref",
                activity.subject_scope_ref,
                result.subject_scope_ref,
            ),
        )
        if expected != actual
    )
    if mismatches:
        raise HarnessValidationError(
            "graph activity result does not match its dispatched identity",
            code="graph_activity_result_identity_mismatch",
            details={"mismatches": list(mismatches)},
        )
    lineage = result.result_lineage
    if lineage is not None:
        expected_graph_version = (
            f"{activity.graph_ref.graph_id}@{activity.graph_ref.identity_version}"
        )
        lineage_mismatches = tuple(
            field_name
            for field_name, expected, actual in (
                ("run_id", activity.run_id, lineage.run_id),
                ("graph_id", activity.graph_ref.graph_id, lineage.graph_id),
                ("graph_version", expected_graph_version, lineage.graph_version),
                ("node_id", activity.node_id, lineage.node_id),
                ("node_instance_id", activity.node_instance_id, lineage.node_instance_id),
                ("attempt", activity.attempt, lineage.attempt),
                ("tenant_scope_ref", activity.tenant_scope_ref, lineage.tenant_scope_ref),
                ("identity_scope_ref", activity.identity_scope_ref, lineage.identity_scope_ref),
                ("subject_scope_ref", activity.subject_scope_ref, lineage.subject_scope_ref),
            )
            if expected != actual
        )
        if lineage_mismatches:
            raise HarnessValidationError(
                "graph result lineage does not match its dispatched activity",
                code="graph_result_lineage_activity_mismatch",
                details={"mismatches": list(lineage_mismatches)},
            )


def _validate_graph_activity_binding(
    activity: HarnessGraphActivity,
    decision_commit: HarnessGraphDecisionCommit,
    state: HarnessGraphState,
) -> None:
    decision = decision_commit.decision
    node = next(
        (
            item
            for item in state.node_instances
            if item.instance_id == activity.node_instance_id
        ),
        None,
    )
    mismatches: list[str] = []
    if decision.decision_type is not HarnessGraphDecisionType.DISPATCH_ACTIVITY:
        mismatches.append("decision_type")
    if decision.run_id != activity.run_id or decision.graph_ref != activity.graph_ref:
        mismatches.append("run_identity")
    if decision_commit.sequence != activity.causal_decision_sequence:
        mismatches.append("causal_decision_sequence")
    if decision_commit.activity_input_ref != activity.input_ref:
        mismatches.append("input_ref")
    if decision.node_id != activity.node_id:
        mismatches.append("node_id")
    if decision.node_instance_id != activity.node_instance_id:
        mismatches.append("node_instance_id")
    if decision.step_ref != activity.step_ref:
        mismatches.append("step_ref")
    if decision.attempt != activity.attempt:
        mismatches.append("attempt")
    if decision.binding_versions.get("step") != activity.step_ref.exact_ref:
        mismatches.append("step_binding")
    if decision.binding_versions.get("worker") != activity.worker_ref.exact_ref:
        mismatches.append("worker_binding")
    if decision.binding_versions.get("activity") != activity.activity_ref.exact_ref:
        mismatches.append("activity_binding")
    if node is None:
        mismatches.append("node_instance")
    else:
        if node.identity.node_id != activity.node_id:
            mismatches.append("node_definition")
        if node.step_ref != activity.step_ref:
            mismatches.append("node_step_ref")
    for field_name in _GRAPH_SCOPE_FIELDS:
        if getattr(activity, field_name) != state.metadata.get(field_name):
            mismatches.append(field_name)
    if mismatches:
        raise EventStoreCorruptionError(
            "graph activity is not exactly bound to its dispatch decision"
        )


def _validate_graph_decision_storage_identity(
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    decision: HarnessGraphDecision,
) -> None:
    definitions = {item.node_id for item in graph.nodes}
    instances = {item.instance_id: item for item in state.node_instances}
    mismatches: list[str] = []
    if decision.run_id != state.run_id:
        mismatches.append("run_id")
    if decision.graph_ref != graph_reference(graph):
        mismatches.append("graph_ref")
    if decision.node_id is not None and decision.node_id not in definitions:
        mismatches.append("node_id")
    if decision.node_instance_id is not None:
        instance = instances.get(decision.node_instance_id)
        if (
            instance is None
            or decision.node_id is None
            or instance.identity.node_id != decision.node_id
        ):
            mismatches.append("node_instance_id")
    if set(decision.target_node_ids).difference(definitions):
        mismatches.append("target_node_ids")
    if mismatches:
        raise HarnessValidationError(
            "graph transition decision is outside its pinned graph state",
            code="graph_transition_decision_identity_mismatch",
            details={"mismatches": sorted(set(mismatches))},
        )


def _counter_delta(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    frozen = freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise HarnessValidationError(
            f"{field_name} must be an object",
            code="invalid_graph_budget_delta",
        )
    normalized: dict[str, int] = {}
    for name, amount in frozen.items():
        normalized[required_text(name, field_name)] = _positive_int(
            amount,
            f"{field_name}.{name}",
        )
    return MappingProxyType(dict(sorted(normalized.items())))


def _datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise HarnessValidationError(
            f"{field_name} must be a datetime",
            code="invalid_graph_commit_time",
        )
    return ensure_utc(value)


def _require_contract_kind(
    value: Any,
    expected: HarnessContractKind,
    field_name: str,
) -> None:
    if not isinstance(value, HarnessContractReference):
        raise TypeError(f"{field_name} must be HarnessContractReference")
    if value.contract_kind is not expected:
        raise HarnessValidationError(
            f"{field_name} uses the wrong contract kind",
            code="graph_activity_contract_kind_mismatch",
            details={"expected": expected.value, "actual": value.contract_kind.value},
        )


def _checksum(value: Any, field_name: str) -> str:
    normalized = required_text(value, field_name)
    if _CHECKSUM_PATTERN.fullmatch(normalized) is None:
        raise HarnessValidationError(
            f"{field_name} must be a canonical sha256 reference",
            code="invalid_graph_runtime_checksum",
        )
    return normalized


def _optional_checksum(value: Any, field_name: str) -> str | None:
    return None if value is None else _checksum(value, field_name)


def _nonnegative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HarnessValidationError(
            f"{field_name} must be a non-negative integer",
            code="invalid_graph_runtime_counter",
        )
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HarnessValidationError(
            f"{field_name} must be a positive integer",
            code="invalid_graph_runtime_counter",
        )
    return value


__all__ = [
    "HARNESS_GRAPH_ACTIVITY_RESULT_SCHEMA",
    "HARNESS_GRAPH_ACTIVITY_SCHEMA",
    "HARNESS_GRAPH_COMMIT_SCHEMA",
    "HARNESS_GRAPH_PROJECTION_RECORD_SCHEMA",
    "HarnessGraphActivity",
    "HarnessGraphActivityResult",
    "HarnessGraphActivityResultCommit",
    "HarnessGraphActivityResultStatus",
    "HarnessGraphCommitKind",
    "HarnessGraphDecisionCommit",
    "HarnessGraphObservationCommit",
    "HarnessGraphProjectionCommit",
    "HarnessGraphRecovery",
    "HarnessGraphTransitionPort",
    "InMemoryHarnessGraphTransitionPort",
    "graph_reference",
    "initial_graph_state",
    "validate_graph_activity_result",
]
