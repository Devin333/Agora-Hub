from __future__ import annotations

"""Immutable contracts used by the Research claim review boundary.

The objects in this module are deliberately independent of persistence and UI.  A
resolver can therefore replay them from a durable record without calling a model.
"""

from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
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


__all__ = [
    "AssertionKind", "CheckExecutionStatus", "CheckResult", "ClaimConclusionStatus",
    "ClaimRevision", "DisputeCase", "DisputeLifecycle", "EvidenceRequirement",
    "RequirementStatus", "ResolutionRecord", "ReviewBinding", "ReviewConclusion",
    "ReviewObservation", "ReviewerType", "CheckObservation",
]
