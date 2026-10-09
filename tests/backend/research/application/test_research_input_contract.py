from __future__ import annotations

from dataclasses import replace

import pytest

from backend.research.application.research_input_contract import (
    ResearchInputContractError,
    ResearchInputContractService,
    research_input_quality_enabled,
)
from backend.research.domain.input_contract import (
    ResearchInputCategory,
    ResearchInputItem,
    ResearchInputProfile,
    ResearchInputStatus,
)


SOURCE = "https://arxiv.org/abs/2606.00123v2"
REVISION = "arxiv:2606.00123v2"


def _item(
    item_id: str,
    category: ResearchInputCategory,
    content_ref: str,
    *,
    status: ResearchInputStatus = ResearchInputStatus.RAW,
    qualification_ref: str | None = None,
) -> ResearchInputItem:
    return ResearchInputItem(
        item_id=item_id,
        category=category,
        content_ref=content_ref,
        source_ref=("task://run-1" if category is ResearchInputCategory.CURRENT_TASK else SOURCE),
        source_revision=("task-revision-1" if category is ResearchInputCategory.CURRENT_TASK else REVISION),
        paper_id="paper-1",
        status=status,
        scope={"condition": "reported test split"},
        qualification_ref=qualification_ref,
    )


def _profile(*extra: ResearchInputItem) -> ResearchInputProfile:
    return ResearchInputProfile(
        profile_id="research.dynamic.v1",
        profile_revision="1",
        tenant_id="tenant-1",
        paper_id="paper-1",
        task_id="task-1",
        task_revision="task-revision-1",
        objective='Explain "method"\nwithout changing the source.',
        constraints=("Use only the reported test split", "Do not infer missing results"),
        items=(
            _item(
                "task",
                ResearchInputCategory.CURRENT_TASK,
                'Explain "method"\nwithout changing the source.',
            ),
            _item("paper", ResearchInputCategory.PAPER_MATERIAL, "document://paper-1"),
            *extra,
        ),
        template_ref="prompt://research/dynamic/v1",
        template_revision="1",
        output_schema_ref="schema://research/analysis/v1",
        budget_input_tokens=4096,
        budget_output_tokens=1024,
    )


class _Qualification:
    def validate_verified_conclusion(self, *, profile, item, gate_version):
        return {
            "eligible": True,
            "qualification_ref": item.qualification_ref,
            "gate_version": gate_version,
            "source_ref": item.source_ref,
            "source_revision": item.source_revision,
            "paper_id": profile.paper_id,
        }


def test_service_prepares_one_checksum_bound_projection_with_all_categories() -> None:
    context = ResearchInputContractService().prepare(
        _profile(),
        tenant_id="tenant-1",
        paper_id="paper-1",
        source_ref=SOURCE,
        source_revision=REVISION,
    )

    assert context.profile.items[0].category is ResearchInputCategory.CURRENT_TASK
    assert set(context.prepared_input.user_payload) == {
        category.value for category in ResearchInputCategory
    }
    envelope = context.candidate_projection()
    assert envelope["projection_checksum"] == context.projection.checksum
    assert envelope["payload"]["objective"] == context.profile.objective
    assert envelope["payload"]["constraints"] == list(context.profile.constraints)


def test_service_rejects_source_or_revision_mismatch_before_preparation() -> None:
    service = ResearchInputContractService()
    with pytest.raises(ResearchInputContractError, match="source_ref mismatch"):
        service.prepare(
            _profile(),
            tenant_id="tenant-1",
            paper_id="paper-1",
            source_ref="https://example.invalid/other",
            source_revision=REVISION,
        )
    with pytest.raises(ResearchInputContractError, match="source_revision mismatch"):
        service.prepare(
            _profile(),
            tenant_id="tenant-1",
            paper_id="paper-1",
            source_ref=SOURCE,
            source_revision="arxiv:2606.00123v1",
        )


def test_verified_conclusion_requires_authoritative_exact_gate_receipt() -> None:
    verified = _item(
        "verified",
        ResearchInputCategory.EXISTING_CONCLUSION,
        "claim://revision-2",
        status=ResearchInputStatus.VERIFIED_CONCLUSION,
        qualification_ref="claim-eligibility://decision-1",
    )
    with pytest.raises(ResearchInputContractError, match="registered qualification"):
        ResearchInputContractService().prepare(
            _profile(verified),
            tenant_id="tenant-1",
            paper_id="paper-1",
            source_ref=SOURCE,
            source_revision=REVISION,
            eligibility_gate_version="ResearchClaimEvidenceEligibilityGate@1",
        )

    context = ResearchInputContractService(
        verified_conclusion_qualification=_Qualification()
    ).prepare(
        _profile(verified),
        tenant_id="tenant-1",
        paper_id="paper-1",
        source_ref=SOURCE,
        source_revision=REVISION,
        eligibility_gate_version="ResearchClaimEvidenceEligibilityGate@1",
    )
    assert context.qualification_receipts[0]["eligible"] is True


def test_model_candidate_cannot_claim_qualification_or_verified_status_outside_conclusion() -> None:
    with pytest.raises(ValueError, match="only verified_conclusion"):
        _item(
            "candidate",
            ResearchInputCategory.TOOL_OBSERVATION,
            "observation://candidate",
            status=ResearchInputStatus.MODEL_CANDIDATE,
            qualification_ref="forged://gate",
        )
    with pytest.raises(ValueError, match="not valid for paper_material"):
        _item(
            "candidate",
            ResearchInputCategory.PAPER_MATERIAL,
            "document://candidate",
            status=ResearchInputStatus.MODEL_CANDIDATE,
        )


def _valid_options(**changes):
    return {
        "research_input_source_revision": REVISION,
        "research_eligibility_gate_version": "ResearchClaimEvidenceEligibilityGate@1",
        **changes,
    }


@pytest.mark.parametrize("profile", [[], ["task"], "profile", 1, True])
def test_explicit_invalid_profile_never_falls_back_to_auto_preparation(profile) -> None:
    with pytest.raises(ResearchInputContractError, match="must be ResearchInputProfile"):
        ResearchInputContractService().prepare_from_options(
            _valid_options(research_input_profile=profile),
            tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
        )


@pytest.mark.parametrize("constraints", [{"keep source": True}, "keep source", b"keep", None, 7, {"keep source"}, [1], [""]])
def test_constraints_reject_non_sequence_or_non_text_values(constraints) -> None:
    with pytest.raises(ResearchInputContractError, match="research_input_constraints"):
        ResearchInputContractService().prepare_from_options(
            _valid_options(research_input_constraints=constraints),
            tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
        )


@pytest.mark.parametrize("objective", [None, 1, True, "", " invalid "])
def test_objective_rejects_non_canonical_text(objective) -> None:
    with pytest.raises(ResearchInputContractError, match="research_input_objective"):
        ResearchInputContractService().prepare_from_options(
            _valid_options(research_input_objective=objective),
            tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
        )


def test_missing_profile_builds_current_task_at_the_task_revision() -> None:
    context = ResearchInputContractService().prepare_from_options(
        _valid_options(), tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
    )
    assert context.profile.items[0].source_revision == context.profile.task_revision
    assert context.profile.items[1].source_revision == REVISION


@pytest.mark.parametrize("field,value", [("user_id", "other-user"), ("tenant_id", "other-tenant"), ("paper_id", "other-paper")])
def test_explicit_scope_cannot_cross_the_accepted_actor(field, value) -> None:
    profile = _profile()
    forged_item = replace(profile.items[1], scope={field: value})
    profile = replace(profile, items=(profile.items[0], forged_item))
    with pytest.raises(ResearchInputContractError, match="scope mismatch"):
        ResearchInputContractService().prepare(
            profile, tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
            source_revision=REVISION, actor_scope={"tenant_id": "tenant-1", "user_id": "user-1"},
        )


def test_accepted_user_scope_is_preserved_in_the_projection() -> None:
    profile = _profile()
    profile = replace(profile, items=(profile.items[0], replace(profile.items[1], scope={"user_id": "user-1"})))
    context = ResearchInputContractService().prepare(
        profile, tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
        source_revision=REVISION, actor_scope={"tenant_id": "tenant-1", "user_id": "user-1"},
    )
    assert context.candidate_projection()["payload"]["actor_scope"]["user_id"] == "user-1"


@pytest.mark.parametrize("version", [None, "ResearchClaimEvidenceEligibilityGate", "ResearchClaimEvidenceEligibilityGate@2", "SummarySchemaGate@1"])
def test_verified_conclusion_requires_the_pinned_qualification_gate(version) -> None:
    verified = _item("verified", ResearchInputCategory.EXISTING_CONCLUSION, "claim://1",
                     status=ResearchInputStatus.VERIFIED_CONCLUSION, qualification_ref="qualification://1")
    with pytest.raises(ResearchInputContractError, match="exact eligibility gate version"):
        ResearchInputContractService(verified_conclusion_qualification=_Qualification()).prepare(
            _profile(verified), tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
            source_revision=REVISION, eligibility_gate_version=version,
        )


@pytest.mark.parametrize("field,value", [("eligible", False), ("qualification_ref", "forged"), ("gate_version", "SummarySchemaGate@1"), ("source_ref", "other-source"), ("source_revision", "old-revision"), ("paper_id", "other-paper")])
def test_verified_receipt_rejects_a_different_source_or_qualification(field, value) -> None:
    class WrongBinding(_Qualification):
        def validate_verified_conclusion(self, **kwargs):
            return {**super().validate_verified_conclusion(**kwargs), field: value}

    verified = _item("verified", ResearchInputCategory.EXISTING_CONCLUSION, "claim://1",
                     status=ResearchInputStatus.VERIFIED_CONCLUSION, qualification_ref="qualification://1")
    with pytest.raises(ResearchInputContractError, match="receipt is invalid"):
        ResearchInputContractService(verified_conclusion_qualification=WrongBinding()).prepare(
            _profile(verified), tenant_id="tenant-1", paper_id="paper-1", source_ref=SOURCE,
            source_revision=REVISION, eligibility_gate_version="ResearchClaimEvidenceEligibilityGate@1",
        )


def test_quality_input_is_disabled_by_default_and_requires_a_boolean() -> None:
    assert research_input_quality_enabled({}) is False
    assert research_input_quality_enabled({"research_input_quality_enabled": False}) is False
    assert research_input_quality_enabled({"research_input_quality_enabled": True}) is True
    for value in (None, "false", 0, 1):
        with pytest.raises(ResearchInputContractError, match="must be boolean"):
            research_input_quality_enabled({"research_input_quality_enabled": value})
