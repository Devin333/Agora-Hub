from __future__ import annotations

"""Research-owned input and context projection contracts.

These contracts describe what Research may project into the existing Harness
context runtime.  They do not grant trust, tool access, or routing authority.
All derived identifiers are deterministic so a prepared request can be
replayed without calling a model or a live source.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from framework.events.canonical import checksum_for


INPUT_CONTRACT_SCHEMA = "newsroom.research-input-contract/v1"
CONTEXT_PROJECTION_SCHEMA = "newsroom.research-context-projection/v1"


class ResearchInputCategory(StrEnum):
    CURRENT_TASK = "current_task"
    PAPER_MATERIAL = "paper_material"
    EXISTING_CONCLUSION = "existing_conclusion"
    TOOL_OBSERVATION = "tool_observation"
    NECESSARY_HISTORY = "necessary_history"


class ResearchInputStatus(StrEnum):
    RAW = "raw"
    AUTHOR_CLAIM = "author_claim"
    MODEL_CANDIDATE = "model_candidate"
    VERIFIED_CONCLUSION = "verified_conclusion"
    UNAVAILABLE = "unavailable"


class ContextRolloverDecision(StrEnum):
    READY = "ready"
    REJECTED = "rejected"


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _refs(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of references")
    result = tuple(_text(item, name) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _scope(value: Mapping[str, Any] | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    normalized = {str(key): _text(item, f"scope[{key}]") for key, item in value.items()}
    return MappingProxyType(normalized)


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class ResearchInputItem:
    """One source-bound item selected by the Research input assembler."""

    item_id: str
    category: ResearchInputCategory | str
    content_ref: str
    source_ref: str
    source_revision: str
    paper_id: str
    status: ResearchInputStatus | str = ResearchInputStatus.RAW
    scope: Mapping[str, Any] = field(default_factory=dict)
    qualification_ref: str | None = None
    content_checksum: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id"))
        object.__setattr__(self, "category", ResearchInputCategory(self.category))
        object.__setattr__(self, "content_ref", _text(self.content_ref, "content_ref"))
        object.__setattr__(self, "source_ref", _text(self.source_ref, "source_ref"))
        object.__setattr__(self, "source_revision", _text(self.source_revision, "source_revision"))
        object.__setattr__(self, "paper_id", _text(self.paper_id, "paper_id"))
        object.__setattr__(self, "status", ResearchInputStatus(self.status))
        object.__setattr__(self, "scope", _scope(self.scope))
        object.__setattr__(self, "qualification_ref", _text(self.qualification_ref, "qualification_ref") if self.qualification_ref is not None else None)
        object.__setattr__(self, "content_checksum", _text(self.content_checksum, "content_checksum") if self.content_checksum is not None else None)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))
        if self.category is ResearchInputCategory.CURRENT_TASK and self.status is not ResearchInputStatus.RAW:
            raise ValueError("current_task input must remain a task contract, not a conclusion")
        if self.status is ResearchInputStatus.VERIFIED_CONCLUSION:
            if self.category is not ResearchInputCategory.EXISTING_CONCLUSION:
                raise ValueError("verified_conclusion status is only valid for existing_conclusion")
            if self.qualification_ref is None:
                raise ValueError("verified_conclusion requires qualification_ref")
        if self.category is ResearchInputCategory.EXISTING_CONCLUSION and self.status is ResearchInputStatus.MODEL_CANDIDATE and self.qualification_ref is not None:
            raise ValueError("model_candidate cannot carry a qualification_ref")
        object.__setattr__(self, "checksum", checksum_for(self.identity_projection()))

    def identity_projection(self) -> dict[str, Any]:
        return {
            "schema": INPUT_CONTRACT_SCHEMA,
            "item_id": self.item_id,
            "category": self.category.value,
            "content_ref": self.content_ref,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "paper_id": self.paper_id,
            "status": self.status.value,
            "scope": dict(self.scope),
            "qualification_ref": self.qualification_ref,
            "content_checksum": self.content_checksum,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_projection(), "checksum": self.checksum, "metadata": dict(self.metadata)}


@dataclass(frozen=True, slots=True)
class ResearchInputProfile:
    """Versioned Research input selection before Harness materialization."""

    profile_id: str
    profile_revision: str
    tenant_id: str
    paper_id: str
    task_id: str
    task_revision: str
    objective: str
    constraints: tuple[str, ...]
    items: tuple[ResearchInputItem, ...]
    template_ref: str
    template_revision: str
    output_schema_ref: str
    budget_input_tokens: int
    budget_output_tokens: int
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("profile_id", "profile_revision", "tenant_id", "paper_id", "task_id", "task_revision", "objective", "template_ref", "template_revision", "output_schema_ref"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        constraints = _refs(self.constraints, "constraints")
        items = tuple(self.items)
        if not items:
            raise ValueError("input profile requires at least one item")
        if not all(isinstance(item, ResearchInputItem) for item in items):
            raise TypeError("items must contain ResearchInputItem values")
        if len({item.item_id for item in items}) != len(items):
            raise ValueError("input item ids must be unique")
        if any(item.paper_id != self.paper_id for item in items):
            raise ValueError("all input items must belong to the profile paper")
        current_tasks = [item for item in items if item.category is ResearchInputCategory.CURRENT_TASK]
        if len(current_tasks) != 1:
            raise ValueError("input profile requires exactly one current_task item")
        if current_tasks[0].content_ref != self.objective:
            raise ValueError("objective must match the current_task content_ref")
        if isinstance(self.budget_input_tokens, bool) or not isinstance(self.budget_input_tokens, int) or self.budget_input_tokens <= 0:
            raise ValueError("budget_input_tokens must be positive")
        if isinstance(self.budget_output_tokens, bool) or not isinstance(self.budget_output_tokens, int) or self.budget_output_tokens <= 0:
            raise ValueError("budget_output_tokens must be positive")
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "checksum", checksum_for(self.identity_projection()))

    def identity_projection(self) -> dict[str, Any]:
        return {
            "schema": INPUT_CONTRACT_SCHEMA,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "tenant_id": self.tenant_id,
            "paper_id": self.paper_id,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "items": [item.to_dict() for item in self.items],
            "template_ref": self.template_ref,
            "template_revision": self.template_revision,
            "output_schema_ref": self.output_schema_ref,
            "budget_input_tokens": self.budget_input_tokens,
            "budget_output_tokens": self.budget_output_tokens,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_projection(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PreparedResearchInput:
    """The final, auditable projection sent to the existing PromptBuilder."""

    profile_checksum: str
    selected_item_ids: tuple[str, ...]
    context_group_refs: tuple[str, ...]
    system_prompt_ref: str
    user_payload: Mapping[str, Any]
    tools_ref: str
    output_schema_ref: str
    prepared_request_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_checksum", _text(self.profile_checksum, "profile_checksum"))
        object.__setattr__(self, "selected_item_ids", _refs(self.selected_item_ids, "selected_item_ids"))
        object.__setattr__(self, "context_group_refs", _refs(self.context_group_refs, "context_group_refs"))
        object.__setattr__(self, "system_prompt_ref", _text(self.system_prompt_ref, "system_prompt_ref"))
        object.__setattr__(self, "tools_ref", _text(self.tools_ref, "tools_ref"))
        object.__setattr__(self, "output_schema_ref", _text(self.output_schema_ref, "output_schema_ref"))
        payload = dict(self.user_payload)
        required = {item.value for item in ResearchInputCategory}
        if set(payload) != required:
            raise ValueError("user_payload must contain exactly the five Research input categories")
        object.__setattr__(self, "user_payload", MappingProxyType(payload))
        object.__setattr__(self, "prepared_request_checksum", checksum_for(self.identity_projection()))

    def identity_projection(self) -> dict[str, Any]:
        return {
            "schema": INPUT_CONTRACT_SCHEMA,
            "profile_checksum": self.profile_checksum,
            "selected_item_ids": list(self.selected_item_ids),
            "context_group_refs": list(self.context_group_refs),
            "system_prompt_ref": self.system_prompt_ref,
            "user_payload": dict(self.user_payload),
            "tools_ref": self.tools_ref,
            "output_schema_ref": self.output_schema_ref,
        }


@dataclass(frozen=True, slots=True)
class ResearchContextProjectionManifest:
    """Research projection that references, but does not own, Harness context."""

    run_id: str
    tenant_id: str
    paper_id: str
    state_revision: str
    window_sequence: int
    context_snapshot_ref: str
    context_snapshot_checksum: str
    archive_manifest_ref: str
    archive_manifest_checksum: str
    selected_item_ids: tuple[str, ...]
    protected_categories: tuple[ResearchInputCategory | str, ...]
    policy_revision: str
    prepared_request_checksum: str
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("run_id", "tenant_id", "paper_id", "state_revision", "context_snapshot_ref", "context_snapshot_checksum", "archive_manifest_ref", "archive_manifest_checksum", "policy_revision", "prepared_request_checksum"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if isinstance(self.window_sequence, bool) or not isinstance(self.window_sequence, int) or self.window_sequence < 0:
            raise ValueError("window_sequence must be a non-negative integer")
        object.__setattr__(self, "selected_item_ids", _refs(self.selected_item_ids, "selected_item_ids"))
        protected = tuple(ResearchInputCategory(item) for item in self.protected_categories)
        if ResearchInputCategory.CURRENT_TASK not in protected:
            raise ValueError("current_task must be protected across rollover")
        object.__setattr__(self, "protected_categories", protected)
        object.__setattr__(self, "checksum", checksum_for(self.identity_projection()))

    def identity_projection(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_PROJECTION_SCHEMA,
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "paper_id": self.paper_id,
            "state_revision": self.state_revision,
            "window_sequence": self.window_sequence,
            "context_snapshot_ref": self.context_snapshot_ref,
            "context_snapshot_checksum": self.context_snapshot_checksum,
            "archive_manifest_ref": self.archive_manifest_ref,
            "archive_manifest_checksum": self.archive_manifest_checksum,
            "selected_item_ids": list(self.selected_item_ids),
            "protected_categories": [item.value for item in self.protected_categories],
            "policy_revision": self.policy_revision,
            "prepared_request_checksum": self.prepared_request_checksum,
        }


def validate_rollover(
    current: ResearchContextProjectionManifest,
    candidate: ResearchContextProjectionManifest,
    *,
    pending_tool_transaction: bool = False,
    indeterminate_attempt: bool = False,
) -> ContextRolloverDecision:
    """Validate a safe projection activation without mutating Harness state."""

    if pending_tool_transaction or indeterminate_attempt:
        raise ValueError("context rollover requires tool transactions and attempts to be reconciled")
    if current.run_id != candidate.run_id or current.tenant_id != candidate.tenant_id or current.paper_id != candidate.paper_id:
        raise ValueError("context rollover cannot change run, tenant, or paper identity")
    if candidate.window_sequence != current.window_sequence + 1:
        raise ValueError("context window sequence must advance exactly once")
    if candidate.state_revision == current.state_revision:
        raise ValueError("context rollover requires a new accepted state revision")
    if candidate.policy_revision != current.policy_revision:
        raise ValueError("run policy is immutable across context rollover")
    if not set(current.protected_categories).issubset(candidate.protected_categories):
        raise ValueError("candidate rollover dropped a protected input category")
    return ContextRolloverDecision.READY


__all__ = [
    "CONTEXT_PROJECTION_SCHEMA",
    "INPUT_CONTRACT_SCHEMA",
    "ContextRolloverDecision",
    "PreparedResearchInput",
    "ResearchContextProjectionManifest",
    "ResearchInputCategory",
    "ResearchInputItem",
    "ResearchInputProfile",
    "ResearchInputStatus",
    "validate_rollover",
]
