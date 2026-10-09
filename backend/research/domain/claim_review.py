from __future__ import annotations

"""Immutable contracts used by the Research claim review boundary.

The objects in this module are deliberately independent of persistence and UI.  A
resolver can therefore replay them from a durable record without calling a model.
"""

from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import math
from types import MappingProxyType, UnionType
from enum import Enum
from typing import Any, Mapping, Union, get_args, get_origin, get_type_hints

from framework.events.canonical import checksum_for


class AssertionKind(StrEnum):
    REPORTED_RESULT = "reported_result"
    EVIDENCE_INTERPRETATION = "evidence_interpretation"
    INDEPENDENT_VALIDATION = "independent_validation"


class RequirementStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class CheckExecutionStatus(StrEnum):
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    INPUT_INVALID = "input_invalid"


class CheckResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ReviewerType(StrEnum):
    HUMAN = "human"
    MODEL = "model"


class ReviewConclusion(StrEnum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ClaimConclusionStatus(StrEnum):
    PENDING = "pending"
    SUPPORTED_WITHIN_SCOPE = "supported_within_scope"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DISPUTED = "disputed"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    SUPERSEDED = "superseded"


class DisputeLifecycle(StrEnum):
    OPEN = "open"
    GATHERING = "gathering"
    WAITING_REVIEW = "waiting_review"
    READY_FOR_RESOLUTION = "ready_for_resolution"
    CLOSED = "closed"


class EvidenceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ObservationProducer(StrEnum):
    WORKER = "worker"
    PARSER = "parser"
    HUMAN = "human"


class ResearchProgressScope(StrEnum):
    RUN = "run"
    CLAIM = "claim"


class ResearchProgressReasonCode(StrEnum):
    INSUFFICIENT_VERIFIED_PROGRESS = "insufficient_verified_progress"


class NumericComparator(StrEnum):
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    EQUAL = "equal"


V1_NUMERIC_COMPARISON_CLAIM_TYPE = "numeric_comparison_result_v1"
INSUFFICIENT_VERIFIED_PROGRESS = ResearchProgressReasonCode.INSUFFICIENT_VERIFIED_PROGRESS.value


def _text(value: str, name: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _scope(value: Mapping[str, str] | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    normalized = {str(k): _text(str(v), f"scope[{k}]") for k, v in value.items()}
    return MappingProxyType(normalized)


def _tuple_text(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    return tuple(_text(v, "reference") for v in (values or ()))


def _sha256_ref(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{name} must be a canonical sha256 reference")
    return value


def _decimal_text(value: str, name: str) -> str:
    normalized = _text(value, name)
    try:
        number = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    canonical = format(number.normalize(), "f")
    return "0" if canonical in {"-0", ""} else canonical


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    if is_dataclass(value):
        return {item.name: _serialize(getattr(value, item.name)) for item in fields(value)}
    return value


class _ContractMixin:
    def to_dict(self) -> dict[str, Any]:
        payload = _serialize(self)
        if not isinstance(payload, dict):  # pragma: no cover - defensive invariant
            raise TypeError("claim review contract must serialize to an object")
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Any:
        """Rehydrate a contract and verify persisted derived checksums."""
        if not isinstance(payload, Mapping):
            raise TypeError("contract payload must be a mapping")
        type_hints = get_type_hints(cls)
        init_fields = {item.name for item in fields(cls) if item.init}
        known_fields = {item.name for item in fields(cls)}
        unknown = set(payload) - known_fields
        if unknown:
            raise ValueError(f"unknown contract fields: {', '.join(sorted(map(str, unknown)))}")
        values = {
            name: _deserialize(type_hints.get(name, Any), payload[name])
            for name in init_fields
            if name in payload
        }
        instance = cls(**values)
        for item in fields(cls):
            if not item.init and item.name in payload and payload[item.name] != getattr(instance, item.name):
                raise ValueError(f"{item.name} does not match contract inputs")
        return instance


def _deserialize(annotation: Any, value: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        options = [item for item in get_args(annotation) if item is not type(None)]
        if value is None:
            return None
        return _deserialize(options[0], value) if options else value
    if origin is tuple:
        item_type = get_args(annotation)[0] if get_args(annotation) else Any
        return tuple(_deserialize(item_type, item) for item in value)
    if origin in (dict, Mapping):
        args = get_args(annotation)
        value_type = args[1] if len(args) > 1 else Any
        return {str(key): _deserialize(value_type, item) for key, item in value.items()}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if isinstance(annotation, type) and is_dataclass(annotation) and isinstance(value, Mapping):
        return annotation.from_dict(value)
    return value


@dataclass(frozen=True, slots=True)
class EvidenceSourceLocator(_ContractMixin):
    source_id: str
    source_revision: str
    source_checksum: str
    locator_type: str
    locator: str
    availability: EvidenceAvailability = EvidenceAvailability.AVAILABLE
    unavailable_reason: str | None = None
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("source_id", "source_revision", "locator_type", "locator"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "source_checksum", _sha256_ref(self.source_checksum, "source_checksum"))
        object.__setattr__(self, "availability", EvidenceAvailability(self.availability))
        if self.availability is EvidenceAvailability.UNAVAILABLE:
            object.__setattr__(self, "unavailable_reason", _text(self.unavailable_reason or "", "unavailable_reason"))
        elif self.unavailable_reason is not None:
            raise ValueError("available source locator cannot carry unavailable_reason")
        object.__setattr__(self, "checksum", checksum_for({
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_checksum": self.source_checksum,
            "locator_type": self.locator_type,
            "locator": self.locator,
            "availability": self.availability.value,
            "unavailable_reason": self.unavailable_reason,
        }))


@dataclass(frozen=True, slots=True)
class NumericComparisonClaim(_ContractMixin):
    metric_id: str
    subject_label: str
    comparator_label: str
    subject_value: str
    comparator_value: str
    unit: str
    comparator: NumericComparator
    difference_value: str
    difference_unit: str
    conditions: Mapping[str, str]
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("metric_id", "subject_label", "comparator_label", "unit", "difference_unit"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in ("subject_value", "comparator_value", "difference_value"):
            object.__setattr__(self, name, _decimal_text(getattr(self, name), name))
        object.__setattr__(self, "comparator", NumericComparator(self.comparator))
        conditions = _scope(self.conditions)
        if not conditions:
            raise ValueError("numeric comparison claim requires applicable conditions")
        object.__setattr__(self, "conditions", conditions)
        actual_difference = Decimal(self.subject_value) - Decimal(self.comparator_value)
        if actual_difference != Decimal(self.difference_value):
            raise ValueError("difference_value must equal subject_value minus comparator_value")
        object.__setattr__(self, "checksum", checksum_for(self._projection()))

    def _projection(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "subject_label": self.subject_label,
            "comparator_label": self.comparator_label,
            "subject_value": self.subject_value,
            "comparator_value": self.comparator_value,
            "unit": self.unit,
            "comparator": self.comparator.value,
            "difference_value": self.difference_value,
            "difference_unit": self.difference_unit,
            "conditions": dict(self.conditions),
        }


@dataclass(frozen=True, slots=True)
class NumericComparisonObservation(_ContractMixin):
    observation_id: str
    claim_checksum: str
    source_locator_checksum: str
    producer_id: str
    producer_type: ObservationProducer
    metric_id: str
    subject_label: str
    comparator_label: str
    subject_value: str
    comparator_value: str
    unit: str
    comparator: NumericComparator
    difference_value: str
    difference_unit: str
    conditions: Mapping[str, str]
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "observation_id", "producer_id", "metric_id", "subject_label",
            "comparator_label", "unit", "difference_unit",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "claim_checksum", _sha256_ref(self.claim_checksum, "claim_checksum"))
        object.__setattr__(self, "source_locator_checksum", _sha256_ref(self.source_locator_checksum, "source_locator_checksum"))
        object.__setattr__(self, "producer_type", ObservationProducer(self.producer_type))
        object.__setattr__(self, "comparator", NumericComparator(self.comparator))
        for name in ("subject_value", "comparator_value", "difference_value"):
            object.__setattr__(self, name, _decimal_text(getattr(self, name), name))
        object.__setattr__(self, "conditions", _scope(self.conditions))
        actual_difference = Decimal(self.subject_value) - Decimal(self.comparator_value)
        if actual_difference != Decimal(self.difference_value):
            raise ValueError("difference_value must equal subject_value minus comparator_value")
        object.__setattr__(self, "checksum", checksum_for({
            "observation_id": self.observation_id,
            "claim_checksum": self.claim_checksum,
            "source_locator_checksum": self.source_locator_checksum,
            "producer_id": self.producer_id,
            "producer_type": self.producer_type.value,
            "metric_id": self.metric_id,
            "subject_label": self.subject_label,
            "comparator_label": self.comparator_label,
            "subject_value": self.subject_value,
            "comparator_value": self.comparator_value,
            "unit": self.unit,
            "comparator": self.comparator.value,
            "difference_value": self.difference_value,
            "difference_unit": self.difference_unit,
            "conditions": dict(self.conditions),
        }))


@dataclass(frozen=True, slots=True)
class ResearchProgressObservation(_ContractMixin):
    run_id: str
    paper_id: str
    observation_scope: ResearchProgressScope
    reason_code: ResearchProgressReasonCode
    metrics: Mapping[str, int | float]
    evidence_context_refs: Mapping[str, str]
    claim_id: str | None = None
    resolver_status: ClaimConclusionStatus | None = None
    observation_only: bool = True
    checksum: str = field(init=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ResearchProgressObservation:
        if not isinstance(payload, Mapping):
            raise TypeError("progress observation payload must be a mapping")
        if "checksum" not in payload:
            raise ValueError("persisted progress observation requires checksum")
        return super(ResearchProgressObservation, cls).from_dict(payload)

    @classmethod
    def insufficient_verified_progress(
        cls,
        *,
        run_id: str,
        paper_id: str,
        evidence_context_refs: Mapping[str, str],
        claim_id: str | None = None,
        resolver_status: ClaimConclusionStatus | None = None,
    ) -> ResearchProgressObservation:
        return cls(
            run_id=run_id,
            paper_id=paper_id,
            observation_scope=(
                ResearchProgressScope.CLAIM
                if claim_id is not None
                else ResearchProgressScope.RUN
            ),
            claim_id=claim_id,
            reason_code=ResearchProgressReasonCode.INSUFFICIENT_VERIFIED_PROGRESS,
            resolver_status=resolver_status,
            metrics={
                "eligible_claims": 0,
                "required_claims": 1,
                "verified_progress_ratio": 0.0,
            },
            evidence_context_refs=evidence_context_refs,
        )

    def __post_init__(self) -> None:
        for name in ("run_id", "paper_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "observation_scope",
            ResearchProgressScope(self.observation_scope),
        )
        object.__setattr__(
            self,
            "reason_code",
            ResearchProgressReasonCode(self.reason_code),
        )
        claim_id = None if self.claim_id is None else _text(self.claim_id, "claim_id")
        object.__setattr__(self, "claim_id", claim_id)
        if self.observation_scope is ResearchProgressScope.CLAIM:
            if claim_id is None:
                raise ValueError("claim-scoped progress observation requires claim_id")
        elif claim_id is not None:
            raise ValueError("run-scoped progress observation cannot carry claim_id")

        resolver_status = (
            None
            if self.resolver_status is None
            else ClaimConclusionStatus(self.resolver_status)
        )
        object.__setattr__(self, "resolver_status", resolver_status)
        if self.observation_scope is ResearchProgressScope.CLAIM and resolver_status is None:
            raise ValueError(
                "claim-scoped progress observation requires resolver_status"
            )
        if self.observation_scope is ResearchProgressScope.RUN and resolver_status is not None:
            raise ValueError(
                "run-scoped progress observation cannot carry resolver_status"
            )

        if not isinstance(self.metrics, Mapping):
            raise TypeError("progress observation metrics must be a mapping")
        expected_metric_names = {
            "eligible_claims",
            "required_claims",
            "verified_progress_ratio",
        }
        if set(self.metrics) != expected_metric_names:
            raise ValueError("progress observation metrics do not match the stable schema")
        eligible_claims = self.metrics["eligible_claims"]
        required_claims = self.metrics["required_claims"]
        for name, value in (
            ("eligible_claims", eligible_claims),
            ("required_claims", required_claims),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer count")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if required_claims < 1:
            raise ValueError("required_claims must be positive")
        if eligible_claims > required_claims:
            raise ValueError("eligible_claims cannot exceed required_claims")
        ratio = self.metrics["verified_progress_ratio"]
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise TypeError("verified_progress_ratio must be numeric")
        if isinstance(ratio, float) and not math.isfinite(ratio):
            raise ValueError("verified_progress_ratio must be finite")
        if not 0.0 <= ratio <= 1.0:
            raise ValueError("verified_progress_ratio must be between zero and one")
        ratio = float(ratio)
        if ratio != eligible_claims / required_claims:
            raise ValueError(
                "verified_progress_ratio must equal eligible_claims / required_claims"
            )
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType({
                "eligible_claims": eligible_claims,
                "required_claims": required_claims,
                "verified_progress_ratio": ratio,
            }),
        )

        if not isinstance(self.evidence_context_refs, Mapping):
            raise TypeError("evidence_context_refs must be a mapping")
        evidence_context_refs: dict[str, str] = {}
        for key, value in self.evidence_context_refs.items():
            if not isinstance(key, str) or not key or key != key.strip():
                raise ValueError("evidence context names must be nonblank canonical strings")
            evidence_context_refs[key] = _sha256_ref(
                value,
                f"evidence_context_refs[{key}]",
            )
        if not evidence_context_refs:
            raise ValueError("progress observation requires evidence context")
        object.__setattr__(
            self,
            "evidence_context_refs",
            MappingProxyType(evidence_context_refs),
        )
        if self.observation_only is not True:
            raise ValueError("Research progress signals must remain observation-only")
        object.__setattr__(self, "checksum", checksum_for({
            "run_id": self.run_id,
            "paper_id": self.paper_id,
            "observation_scope": self.observation_scope.value,
            "claim_id": self.claim_id,
            "reason_code": self.reason_code.value,
            "resolver_status": (
                self.resolver_status.value
                if self.resolver_status is not None
                else None
            ),
            "metrics": dict(self.metrics),
            "evidence_context_refs": dict(self.evidence_context_refs),
            "observation_only": True,
        }))


@dataclass(frozen=True, slots=True)
class ClaimRevision(_ContractMixin):
    claim_id: str
    revision: int
    text: str
    assertion_kind: AssertionKind
    claim_type: str
    scope: Mapping[str, str] = field(default_factory=dict)
    objective_revision: str = ""
    predecessor_revision: int | None = None
    change_reason: str | None = None
    text_checksum: str = field(init=False)
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _text(self.claim_id, "claim_id"))
        if self.revision < 1:
            raise ValueError("revision must be positive")
        object.__setattr__(self, "text", _text(self.text, "text"))
        object.__setattr__(self, "claim_type", _text(self.claim_type, "claim_type"))
        object.__setattr__(self, "assertion_kind", AssertionKind(self.assertion_kind))
        scope = _scope(self.scope)
        if scope.get("paper_id") is None:
            raise ValueError("claim scope requires paper_id")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "objective_revision", _text(self.objective_revision, "objective_revision"))
        if self.revision == 1:
            if self.predecessor_revision is not None:
                raise ValueError("first claim revision cannot have a predecessor")
            if self.change_reason is not None:
                raise ValueError("first claim revision cannot have change_reason")
        else:
            if self.predecessor_revision != self.revision - 1:
                raise ValueError("claim revisions must point to the immediately previous revision")
            object.__setattr__(self, "change_reason", _text(self.change_reason or "", "change_reason"))
        object.__setattr__(self, "text_checksum", checksum_for({"text": self.text}))
        object.__setattr__(self, "checksum", checksum_for(self._projection()))

    def _projection(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "revision": self.revision,
            "text_checksum": self.text_checksum,
            "assertion_kind": self.assertion_kind.value,
            "claim_type": self.claim_type,
            "scope": dict(self.scope),
            "objective_revision": self.objective_revision,
            "predecessor_revision": self.predecessor_revision,
            "change_reason": self.change_reason,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRequirement(_ContractMixin):
    requirement_id: str
    requirement_type: str
    status: RequirementStatus
    applicable: bool = True
    evidence_refs: tuple[str, ...] = ()
    rationale: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _text(self.requirement_id, "requirement_id"))
        object.__setattr__(self, "requirement_type", _text(self.requirement_type, "requirement_type"))
        object.__setattr__(self, "status", RequirementStatus(self.status))
        object.__setattr__(self, "evidence_refs", _tuple_text(self.evidence_refs))
        if not isinstance(self.applicable, bool):
            raise TypeError("applicable must be a bool")
        if self.status is RequirementStatus.NOT_APPLICABLE and self.applicable:
            raise ValueError("not_applicable requirement must set applicable=False")
        if self.status is not RequirementStatus.NOT_APPLICABLE and not self.applicable:
            raise ValueError("only not_applicable requirements may set applicable=False")
        if self.status is RequirementStatus.NOT_APPLICABLE:
            object.__setattr__(self, "rationale", _text(self.rationale or "", "rationale"))
        elif self.rationale is not None:
            object.__setattr__(self, "rationale", str(self.rationale).strip() or None)
        if self.status is RequirementStatus.NOT_APPLICABLE and self.evidence_refs:
            raise ValueError("not_applicable requirement cannot carry evidence_refs")
        if self.status is RequirementStatus.MISSING and self.evidence_refs:
            raise ValueError("missing requirement cannot carry evidence_refs")
        if self.applicable and self.status is RequirementStatus.PRESENT and not self.evidence_refs:
            raise ValueError("present requirement requires evidence_refs")

    @property
    def required(self) -> bool:
        return self.applicable and self.status is not RequirementStatus.NOT_APPLICABLE


@dataclass(frozen=True, slots=True)
class ReviewBinding(_ContractMixin):
    tenant_id: str
    actor_scope: Mapping[str, str]
    run_id: str
    paper_id: str
    paper_snapshot_checksum: str
    evidence_revision: str
    evidence_checksum: str
    claim: ClaimRevision
    rule_version: str
    strategy_version: str
    evidence_locators: tuple[EvidenceSourceLocator, ...] = ()
    applicable_conditions: Mapping[str, str] = field(default_factory=dict)
    requires_independent_human_review: bool | None = None
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("tenant_id", "run_id", "paper_id", "paper_snapshot_checksum", "evidence_revision", "evidence_checksum", "rule_version", "strategy_version"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not isinstance(self.claim, ClaimRevision):
            raise TypeError("claim must be a ClaimRevision")
        if self.claim.scope.get("paper_id") != self.paper_id:
            raise ValueError("claim scope paper_id does not match paper_id")
        scope = dict(_scope(self.actor_scope))
        if scope.get("tenant_id", self.tenant_id) != self.tenant_id:
            raise ValueError("actor_scope tenant_id does not match tenant_id")
        scope["tenant_id"] = self.tenant_id
        object.__setattr__(self, "actor_scope", MappingProxyType(scope))
        locators = tuple(self.evidence_locators)
        if any(not isinstance(locator, EvidenceSourceLocator) for locator in locators):
            raise TypeError("evidence_locators must contain EvidenceSourceLocator values")
        object.__setattr__(self, "evidence_locators", locators)
        object.__setattr__(self, "applicable_conditions", _scope(self.applicable_conditions))
        if self.requires_independent_human_review is None:
            object.__setattr__(self, "requires_independent_human_review", self._review_policy())
        elif self.requires_independent_human_review is not self._review_policy():
            raise ValueError("requires_independent_human_review does not match frozen review policy")
        object.__setattr__(self, "checksum", checksum_for(self._projection()))

    def _review_policy(self) -> bool:
        if self.claim.assertion_kind is not AssertionKind.REPORTED_RESULT:
            return True
        return self.claim.claim_type.casefold() in {"experiment", "experimental", "sota"}

    def _projection(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "actor_scope": dict(self.actor_scope),
            "run_id": self.run_id,
            "paper_id": self.paper_id,
            "paper_snapshot_checksum": self.paper_snapshot_checksum,
            "evidence_revision": self.evidence_revision,
            "evidence_checksum": self.evidence_checksum,
            "claim_checksum": self.claim.checksum,
            "rule_version": self.rule_version,
            "strategy_version": self.strategy_version,
            "evidence_locator_checksums": tuple(locator.checksum for locator in self.evidence_locators),
            "applicable_conditions": dict(self.applicable_conditions),
            "requires_independent_human_review": self.requires_independent_human_review,
        }


@dataclass(frozen=True, slots=True)
class CheckObservation(_ContractMixin):
    check_id: str
    binding_checksum: str
    checker_id: str
    checker_version: str
    execution_status: CheckExecutionStatus
    result: CheckResult | None = None
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    input_checksum: str | None = None

    def __post_init__(self) -> None:
        for name in ("check_id", "binding_checksum", "checker_id", "checker_version"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "execution_status", CheckExecutionStatus(self.execution_status))
        if self.result is not None:
            object.__setattr__(self, "result", CheckResult(self.result))
        object.__setattr__(self, "reason_codes", _tuple_text(self.reason_codes))
        object.__setattr__(self, "evidence_refs", _tuple_text(self.evidence_refs))
        if self.execution_status is CheckExecutionStatus.COMPLETED and self.result is None:
            raise ValueError("completed check requires result")
        if self.execution_status is not CheckExecutionStatus.COMPLETED and self.result is not None:
            raise ValueError("non-completed check cannot have a result")


@dataclass(frozen=True, slots=True)
class ReviewObservation(_ContractMixin):
    observation_id: str
    binding_checksum: str
    reviewer_id: str
    reviewer_type: ReviewerType
    conclusion: ReviewConclusion
    rationale: str
    evidence_refs: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    scope: Mapping[str, str] = field(default_factory=dict)
    supersedes_observation_id: str | None = None
    substantive: bool = True

    def __post_init__(self) -> None:
        for name in ("observation_id", "binding_checksum", "reviewer_id", "rationale"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "reviewer_type", ReviewerType(self.reviewer_type))
        object.__setattr__(self, "conclusion", ReviewConclusion(self.conclusion))
        object.__setattr__(self, "evidence_refs", _tuple_text(self.evidence_refs))
        object.__setattr__(self, "missing_requirements", _tuple_text(self.missing_requirements))
        object.__setattr__(self, "scope", _scope(self.scope))
        if self.conclusion is not ReviewConclusion.SUPPORTS and not self.evidence_refs and not self.missing_requirements:
            raise ValueError("non-supporting review requires evidence or a missing requirement")
        if not self.substantive:
            raise ValueError("review observation must contain substantive grounds")


@dataclass(frozen=True, slots=True)
class DisputeCase(_ContractMixin):
    case_id: str
    binding_checksum: str
    lifecycle: DisputeLifecycle
    issue_kind: str
    question: str
    revision: int = 1
    unresolved_items: tuple[str, ...] = ()
    close_reason: str | None = None
    budget_exhausted: bool = False

    def __post_init__(self) -> None:
        for name in ("case_id", "binding_checksum", "issue_kind", "question"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "lifecycle", DisputeLifecycle(self.lifecycle))
        object.__setattr__(self, "unresolved_items", _tuple_text(self.unresolved_items))
        if self.revision < 1:
            raise ValueError("case revision must be positive")
        if self.lifecycle is DisputeLifecycle.CLOSED and not self.close_reason:
            raise ValueError("closed dispute requires close_reason")


@dataclass(frozen=True, slots=True)
class NumericComparisonEligibilityBundle(_ContractMixin):
    binding: ReviewBinding
    claim_contract: NumericComparisonClaim
    observations: tuple[NumericComparisonObservation, ...]
    reviews: tuple[ReviewObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ReviewBinding):
            raise TypeError("binding must be a ReviewBinding")
        if not isinstance(self.claim_contract, NumericComparisonClaim):
            raise TypeError("claim_contract must be a NumericComparisonClaim")
        observations = tuple(self.observations)
        reviews = tuple(self.reviews)
        if any(not isinstance(item, NumericComparisonObservation) for item in observations):
            raise TypeError("observations must contain NumericComparisonObservation values")
        if any(not isinstance(item, ReviewObservation) for item in reviews):
            raise TypeError("reviews must contain ReviewObservation values")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "reviews", reviews)


@dataclass(frozen=True, slots=True)
class ResolutionRecord(_ContractMixin):
    binding_checksum: str
    claim_id: str
    claim_revision: int
    status: ClaimConclusionStatus
    check_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    rule_version: str
    reasons: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("binding_checksum", "claim_id", "rule_version"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "status", ClaimConclusionStatus(self.status))
        if self.claim_revision < 1:
            raise ValueError("claim_revision must be positive")
        for name in ("check_ids", "observation_ids", "reasons", "unresolved_items"):
            object.__setattr__(self, name, _tuple_text(getattr(self, name)))
        object.__setattr__(self, "checksum", checksum_for({
            "binding_checksum": self.binding_checksum,
            "claim_id": self.claim_id,
            "claim_revision": self.claim_revision,
            "status": self.status.value,
            "check_ids": self.check_ids,
            "observation_ids": self.observation_ids,
            "rule_version": self.rule_version,
            "reasons": self.reasons,
            "unresolved_items": self.unresolved_items,
        }))


@dataclass(frozen=True, slots=True)
class ClaimEligibilityDecision(_ContractMixin):
    binding_checksum: str
    claim_id: str
    claim_revision: int
    claim_contract_checksum: str
    resolution: ResolutionRecord
    resolver_ref: str
    gate_ref: str
    source_locator_checksums: tuple[str, ...]
    typed_observation_checksums: tuple[str, ...]
    eligible: bool
    reason_codes: tuple[str, ...] = ()
    progress_observations: tuple[ResearchProgressObservation, ...] = ()
    checksum: str = field(init=False)
    qualification_ref: str | None = field(init=False)

    def __post_init__(self) -> None:
        for name in ("binding_checksum", "claim_id", "claim_contract_checksum", "resolver_ref", "gate_ref"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if "@" not in self.resolver_ref or "@" not in self.gate_ref:
            raise ValueError("resolver_ref and gate_ref must include exact versions")
        if self.claim_revision < 1:
            raise ValueError("claim_revision must be positive")
        if not isinstance(self.resolution, ResolutionRecord):
            raise TypeError("resolution must be a ResolutionRecord")
        if not isinstance(self.eligible, bool):
            raise TypeError("eligible must be a bool")
        for name in ("source_locator_checksums", "typed_observation_checksums", "reason_codes"):
            object.__setattr__(self, name, _tuple_text(getattr(self, name)))
        progress = tuple(self.progress_observations)
        if any(not isinstance(item, ResearchProgressObservation) for item in progress):
            raise TypeError("progress_observations must contain ResearchProgressObservation values")
        object.__setattr__(self, "progress_observations", progress)
        if self.eligible and progress:
            raise ValueError("eligible decision cannot carry insufficient progress")
        for observation in progress:
            if observation.observation_scope is not ResearchProgressScope.CLAIM:
                raise ValueError("eligibility decision progress must be claim-scoped")
            if observation.claim_id != self.claim_id:
                raise ValueError("progress observation belongs to a different claim")
            if observation.resolver_status is not self.resolution.status:
                raise ValueError("progress resolver status does not match resolution")
        if self.binding_checksum != self.resolution.binding_checksum:
            raise ValueError("resolution belongs to a different review binding")
        if self.claim_id != self.resolution.claim_id or self.claim_revision != self.resolution.claim_revision:
            raise ValueError("resolution belongs to a different claim revision")
        if self.eligible is not (self.resolution.status is ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE):
            raise ValueError("eligible must be derived from the resolver status")
        checksum = checksum_for({
            "binding_checksum": self.binding_checksum,
            "claim_id": self.claim_id,
            "claim_revision": self.claim_revision,
            "claim_contract_checksum": self.claim_contract_checksum,
            "resolution_checksum": self.resolution.checksum,
            "resolver_ref": self.resolver_ref,
            "gate_ref": self.gate_ref,
            "source_locator_checksums": self.source_locator_checksums,
            "typed_observation_checksums": self.typed_observation_checksums,
            "eligible": self.eligible,
            "reason_codes": self.reason_codes,
            "progress_observation_checksums": tuple(item.checksum for item in progress),
        })
        object.__setattr__(self, "checksum", checksum)
        object.__setattr__(
            self,
            "qualification_ref",
            f"claim-qualification:{checksum}" if self.eligible else None,
        )


__all__ = [
    "AssertionKind", "CheckExecutionStatus", "CheckResult", "ClaimConclusionStatus",
    "ClaimEligibilityDecision", "ClaimRevision", "DisputeCase", "DisputeLifecycle", "EvidenceAvailability",
    "EvidenceRequirement", "EvidenceSourceLocator", "INSUFFICIENT_VERIFIED_PROGRESS",
    "NumericComparator", "NumericComparisonClaim", "NumericComparisonEligibilityBundle", "NumericComparisonObservation",
    "ObservationProducer", "RequirementStatus", "ResearchProgressObservation",
    "ResearchProgressReasonCode", "ResearchProgressScope",
    "ResolutionRecord", "ReviewBinding", "ReviewConclusion", "ReviewObservation",
    "ReviewerType", "CheckObservation", "V1_NUMERIC_COMPARISON_CLAIM_TYPE",
]
