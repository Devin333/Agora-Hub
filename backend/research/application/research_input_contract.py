from __future__ import annotations

"""Application-owned preparation of controlled Research model input."""

from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any, Mapping, Protocol, runtime_checkable

from backend.research.domain.input_contract import (
    PreparedResearchInput,
    ResearchInputCategory,
    ResearchInputItem,
    ResearchInputProfile,
    ResearchInputStatus,
)
from backend.research.services.claim_evidence_eligibility import (
    RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
)
from framework.agent.loop.prompt import PreparedInputProjection


RESEARCH_INPUT_QUALITY_OPTION = "research_input_quality_enabled"
RESEARCH_INPUT_PROFILE_OPTION = "research_input_profile"
RESEARCH_INPUT_SOURCE_REVISION_OPTION = "research_input_source_revision"
RESEARCH_ELIGIBILITY_GATE_VERSION_OPTION = "research_eligibility_gate_version"
RESEARCH_INPUT_PROJECTION_ID = "research.single-paper.input"
RESEARCH_INPUT_PROJECTION_REVISION = "1"


class ResearchInputContractError(ValueError):
    """Raised before source or worker dispatch when an opted-in profile is invalid."""

    code = "research_input_contract_invalid"


@runtime_checkable
class ResearchVerifiedConclusionQualificationPort(Protocol):
    """Deterministically validate one claimed verified conclusion binding."""

    def validate_verified_conclusion(
        self,
        *,
        profile: ResearchInputProfile,
        item: ResearchInputItem,
        gate_version: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PreparedResearchInputContext:
    profile: ResearchInputProfile
    prepared_input: PreparedResearchInput
    projection: PreparedInputProjection
    qualification_receipts: tuple[Mapping[str, Any], ...] = ()

    def candidate_projection(self) -> dict[str, Any]:
        return self.projection.project({})[self.projection.input_key]


class ResearchInputContractService:
    """Build and validate the one controlled input projection for a run."""

    def __init__(
        self,
        *,
        verified_conclusion_qualification: (
            ResearchVerifiedConclusionQualificationPort | None
        ) = None,
    ) -> None:
        if verified_conclusion_qualification is not None and not isinstance(
            verified_conclusion_qualification,
            ResearchVerifiedConclusionQualificationPort,
        ):
            raise TypeError(
                "verified_conclusion_qualification must implement "
                "ResearchVerifiedConclusionQualificationPort"
            )
        self._verified_conclusion_qualification = verified_conclusion_qualification

    def prepare_from_options(
        self,
        options: Mapping[str, Any],
        *,
        tenant_id: str,
        paper_id: str,
        source_ref: str,
        actor_scope: Mapping[str, Any] | None = None,
    ) -> PreparedResearchInputContext:
        if not isinstance(options, Mapping):
            raise TypeError("options must be a mapping")
        constraints = options.get("research_input_constraints", ())
        if not isinstance(constraints, Sequence) or isinstance(constraints, (str, bytes)):
            raise ResearchInputContractError(
                "research_input_constraints must be a sequence of canonical text"
            )
        constraints = tuple(
            _canonical_option_text(value, "research_input_constraints")
            for value in constraints
        )
        if "research_input_objective" in options:
            _canonical_option_text(options["research_input_objective"], "research_input_objective")
        raw_profile = options.get(RESEARCH_INPUT_PROFILE_OPTION)
        if isinstance(raw_profile, ResearchInputProfile):
            profile = raw_profile
        elif isinstance(raw_profile, Mapping):
            try:
                profile = ResearchInputProfile.from_dict(raw_profile)
            except (TypeError, ValueError) as exc:
                raise ResearchInputContractError(
                    "research_input_profile is invalid"
                ) from exc
        elif raw_profile is None:
            source_revision = _canonical_option_text(
                options.get(RESEARCH_INPUT_SOURCE_REVISION_OPTION),
                RESEARCH_INPUT_SOURCE_REVISION_OPTION,
            )
            objective = _canonical_option_text(
                options.get("research_input_objective", f"Analyze paper {paper_id}"),
                "research_input_objective",
            )
            try:
                profile = self.build_profile(
                    tenant_id=tenant_id,
                    paper_id=paper_id,
                    source_ref=source_ref,
                    source_revision=source_revision,
                    objective=objective,
                    constraints=tuple(constraints),
                )
            except (TypeError, ValueError) as exc:
                raise ResearchInputContractError(
                    "Research input profile options are invalid"
                ) from exc
        else:
            raise ResearchInputContractError(
                "research_input_profile must be ResearchInputProfile or a mapping"
            )
        if (
            "research_input_objective" in options
            and profile.objective != options["research_input_objective"]
        ):
            raise ResearchInputContractError("input profile objective mismatch")
        if "research_input_constraints" in options and profile.constraints != constraints:
            raise ResearchInputContractError("input profile constraints mismatch")
        profile_revisions = {
            item.source_revision
            for item in profile.items
            if item.category is not ResearchInputCategory.CURRENT_TASK
        }
        if len(profile_revisions) != 1:
            raise ResearchInputContractError(
                "research_input_profile must bind one source revision"
            )
        source_revision = _canonical_option_text(
            options.get(RESEARCH_INPUT_SOURCE_REVISION_OPTION, next(iter(profile_revisions))),
            RESEARCH_INPUT_SOURCE_REVISION_OPTION,
        )
        gate_version = _canonical_option_text(
            options.get(RESEARCH_ELIGIBILITY_GATE_VERSION_OPTION),
            RESEARCH_ELIGIBILITY_GATE_VERSION_OPTION,
        )
        return self.prepare(
            profile,
            tenant_id=tenant_id,
            paper_id=paper_id,
            source_ref=source_ref,
            source_revision=source_revision,
            eligibility_gate_version=gate_version,
            actor_scope=actor_scope,
        )

    @staticmethod
    def build_profile(
        *,
        tenant_id: str,
        paper_id: str,
        source_ref: str,
        source_revision: str,
        objective: str,
        constraints: tuple[str, ...],
        task_id: str = "research-single-paper",
        task_revision: str = "1",
    ) -> ResearchInputProfile:
        return ResearchInputProfile(
            profile_id=RESEARCH_INPUT_PROJECTION_ID,
            profile_revision=RESEARCH_INPUT_PROJECTION_REVISION,
            tenant_id=tenant_id,
            paper_id=paper_id,
            task_id=task_id,
            task_revision=task_revision,
            objective=objective,
            constraints=constraints,
            items=(
                ResearchInputItem(
                    item_id="current-task",
                    category=ResearchInputCategory.CURRENT_TASK,
                    content_ref=objective,
                    source_ref=source_ref,
                    source_revision=task_revision,
                    paper_id=paper_id,
                    status=ResearchInputStatus.RAW,
                ),
                ResearchInputItem(
                    item_id="paper-material",
                    category=ResearchInputCategory.PAPER_MATERIAL,
                    content_ref=f"paper://{paper_id}",
                    source_ref=source_ref,
                    source_revision=source_revision,
                    paper_id=paper_id,
                    status=ResearchInputStatus.RAW,
                ),
            ),
            template_ref="prompt://research/dynamic-single-paper/v1",
            template_revision=RESEARCH_INPUT_PROJECTION_REVISION,
            output_schema_ref="schema://research/analysis/v1",
            budget_input_tokens=4096,
            budget_output_tokens=1024,
        )

    def prepare(
        self,
        profile: ResearchInputProfile,
        *,
        tenant_id: str,
        paper_id: str,
        source_ref: str,
        source_revision: str,
        eligibility_gate_version: str | None = None,
        actor_scope: Mapping[str, Any] | None = None,
    ) -> PreparedResearchInputContext:
        if not isinstance(profile, ResearchInputProfile):
            raise TypeError("profile must be ResearchInputProfile")
        expected_tenant = _canonical_option_text(tenant_id, "tenant_id")
        expected_paper = _canonical_option_text(paper_id, "paper_id")
        expected_source = _canonical_option_text(source_ref, "source_ref")
        expected_revision = _canonical_option_text(source_revision, "source_revision")
        if profile.tenant_id != expected_tenant:
            raise ResearchInputContractError("input profile tenant mismatch")
        if profile.paper_id != expected_paper:
            raise ResearchInputContractError("input profile paper mismatch")

        if actor_scope is not None and not isinstance(actor_scope, Mapping):
            raise ResearchInputContractError("actor_scope must be a mapping")
        accepted_scope = dict(actor_scope or {})
        if accepted_scope.get("tenant_id", expected_tenant) != expected_tenant:
            raise ResearchInputContractError("input actor tenant mismatch")
        accepted_scope["tenant_id"] = expected_tenant
        accepted_scope["paper_id"] = expected_paper
        accepted_scope["source_ref"] = expected_source
        accepted_scope["source_revision"] = expected_revision
        for item in profile.items:
            for name in ("tenant_id", "user_id", "memory_namespace", "paper_id"):
                if name in item.scope and item.scope[name] != accepted_scope.get(name):
                    raise ResearchInputContractError(
                        f"input item {item.item_id!r} {name} scope mismatch"
                    )

        source_bound_items = tuple(
            item
            for item in profile.items
            if item.category is not ResearchInputCategory.CURRENT_TASK
        )
        if not source_bound_items:
            raise ResearchInputContractError(
                "input profile requires source-bound paper material"
            )
        for item in source_bound_items:
            if item.source_ref != expected_source:
                raise ResearchInputContractError(
                    f"input item {item.item_id!r} source_ref mismatch"
                )
            if item.source_revision != expected_revision:
                raise ResearchInputContractError(
                    f"input item {item.item_id!r} source_revision mismatch"
                )

        receipts: list[Mapping[str, Any]] = []
        for item in profile.items:
            if item.status is not ResearchInputStatus.VERIFIED_CONCLUSION:
                continue
            if eligibility_gate_version != RESEARCH_CLAIM_ELIGIBILITY_GATE_REF:
                raise ResearchInputContractError(
                    "verified conclusion requires an exact eligibility gate version"
                )
            validator = self._verified_conclusion_qualification
            if validator is None:
                raise ResearchInputContractError(
                    "verified conclusion requires a registered qualification service"
                )
            try:
                receipt = dict(
                    validator.validate_verified_conclusion(
                        profile=profile,
                        item=item,
                        gate_version=eligibility_gate_version,
                    )
                )
            except Exception as exc:
                raise ResearchInputContractError(
                    f"verified conclusion {item.item_id!r} qualification failed"
                ) from exc
            self._validate_qualification_receipt(
                receipt,
                item=item,
                gate_version=eligibility_gate_version,
                source_ref=expected_source,
                source_revision=expected_revision,
            )
            receipts.append(receipt)

        grouped: dict[str, list[dict[str, Any]]] = {
            category.value: [] for category in ResearchInputCategory
        }
        for item in profile.items:
            grouped[item.category.value].append(item.to_dict())
        prepared = PreparedResearchInput(
            profile_checksum=profile.checksum,
            selected_item_ids=tuple(item.item_id for item in profile.items),
            context_group_refs=tuple(
                f"research-input://{profile.profile_id}/{category.value}"
                for category in ResearchInputCategory
                if grouped[category.value]
            ),
            system_prompt_ref=profile.template_ref,
            user_payload=grouped,
            tools_ref="tools://research/dynamic-single-paper/v1",
            output_schema_ref=profile.output_schema_ref,
        )
        projection_payload = {
            "schema": "newsroom.research-prepared-input/v1",
            "profile_id": profile.profile_id,
            "profile_revision": profile.profile_revision,
            "profile_checksum": profile.checksum,
            "task_id": profile.task_id,
            "task_revision": profile.task_revision,
            "objective": profile.objective,
            "constraints": list(profile.constraints),
            "source_ref": expected_source,
            "source_revision": expected_revision,
            "actor_scope": accepted_scope,
            "prepared_input": prepared.to_dict(),
            "eligibility_gate_version": eligibility_gate_version,
            "qualification_receipts": [dict(receipt) for receipt in receipts],
        }
        projection = PreparedInputProjection(
            projection_id=RESEARCH_INPUT_PROJECTION_ID,
            projection_revision=RESEARCH_INPUT_PROJECTION_REVISION,
            payload=projection_payload,
            input_key="research_input",
        )
        return PreparedResearchInputContext(
            profile=profile,
            prepared_input=prepared,
            projection=projection,
            qualification_receipts=tuple(receipts),
        )

    @staticmethod
    def _validate_qualification_receipt(
        receipt: Mapping[str, Any],
        *,
        item: ResearchInputItem,
        gate_version: str,
        source_ref: str,
        source_revision: str,
    ) -> None:
        expected = {
            "eligible": True,
            "qualification_ref": item.qualification_ref,
            "gate_version": gate_version,
            "source_ref": source_ref,
            "source_revision": source_revision,
            "paper_id": item.paper_id,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ResearchInputContractError(
                f"verified conclusion {item.item_id!r} qualification receipt is invalid"
            )


def research_input_quality_enabled(options: Mapping[str, Any]) -> bool:
    value = options.get(RESEARCH_INPUT_QUALITY_OPTION, False)
    if type(value) is not bool:
        raise ResearchInputContractError(
            f"{RESEARCH_INPUT_QUALITY_OPTION} must be boolean"
        )
    return value


def _canonical_option_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ResearchInputContractError(f"{name} must be non-empty canonical text")
    return value


__all__ = [
    "PreparedResearchInputContext",
    "RESEARCH_ELIGIBILITY_GATE_VERSION_OPTION",
    "RESEARCH_INPUT_PROFILE_OPTION",
    "RESEARCH_INPUT_QUALITY_OPTION",
    "RESEARCH_INPUT_SOURCE_REVISION_OPTION",
    "ResearchInputContractError",
    "ResearchInputContractService",
    "ResearchVerifiedConclusionQualificationPort",
    "research_input_quality_enabled",
]
