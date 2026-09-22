from __future__ import annotations

import pytest

from backend.research.domain import (
    ContextRolloverDecision,
    PreparedResearchInput,
    ResearchContextProjectionManifest,
    ResearchInputCategory,
    ResearchInputItem,
    ResearchInputProfile,
    ResearchInputStatus,
    validate_rollover,
)


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
        source_ref=f"source://{item_id}",
        source_revision="snapshot-1",
        paper_id="paper-1",
        status=status,
        qualification_ref=qualification_ref,
    )


def _profile(*items: ResearchInputItem) -> ResearchInputProfile:
    return ResearchInputProfile(
        profile_id="research.dynamic.v1",
        profile_revision="1",
        tenant_id="tenant-1",
        paper_id="paper-1",
        task_id="task-1",
        task_revision="task-revision-1",
        objective="Explain the method",
        constraints=("Do not evaluate results",),
        items=items,
        template_ref="prompt://research",
        template_revision="template-1",
        output_schema_ref="schema://research-answer/v1",
        budget_input_tokens=2000,
        budget_output_tokens=500,
    )


def _manifest(*, window_sequence: int, state_revision: str = "state-1", policy_revision: str = "policy-1") -> ResearchContextProjectionManifest:
    return ResearchContextProjectionManifest(
        run_id="run-1",
        tenant_id="tenant-1",
        paper_id="paper-1",
        state_revision=state_revision,
        window_sequence=window_sequence,
        context_snapshot_ref=f"context://snapshot-{window_sequence}",
        context_snapshot_checksum=f"snapshot-checksum-{window_sequence}",
        archive_manifest_ref=f"archive://manifest-{window_sequence}",
        archive_manifest_checksum=f"archive-checksum-{window_sequence}",
        selected_item_ids=("task", "paper"),
        protected_categories=(ResearchInputCategory.CURRENT_TASK, ResearchInputCategory.PAPER_MATERIAL),
        policy_revision=policy_revision,
        prepared_request_checksum=f"request-checksum-{window_sequence}",
    )


def test_profile_requires_one_source_owned_current_task_and_matching_objective() -> None:
    task = _item("task", ResearchInputCategory.CURRENT_TASK, "Explain the method")
    profile = _profile(task, _item("paper", ResearchInputCategory.PAPER_MATERIAL, "source://method"))

    assert profile.items[0].category is ResearchInputCategory.CURRENT_TASK
    assert profile.checksum.startswith("sha256:")
    assert profile.items[0].checksum.startswith("sha256:")

    with pytest.raises(ValueError, match="objective must match"):
        _profile(_item("task", ResearchInputCategory.CURRENT_TASK, "A different objective"))


def test_verified_conclusion_requires_research_qualification_and_candidate_cannot_self_upgrade() -> None:
    with pytest.raises(ValueError, match="qualification_ref"):
        _item(
            "conclusion",
            ResearchInputCategory.EXISTING_CONCLUSION,
            "claim://1",
            status=ResearchInputStatus.VERIFIED_CONCLUSION,
        )
    with pytest.raises(ValueError, match="qualification_ref"):
        _item(
            "candidate",
            ResearchInputCategory.EXISTING_CONCLUSION,
            "claim://1",
            status=ResearchInputStatus.MODEL_CANDIDATE,
            qualification_ref="gate://forged",
        )
    with pytest.raises(TypeError):
        ResearchInputItem(  # type: ignore[call-arg]
            item_id="forged",
            category="verified",
            content_ref="claim://1",
            source_ref="source://1",
            source_revision="snapshot-1",
            paper_id="paper-1",
            verified=True,
        )


def test_prepared_input_requires_all_categories_and_deterministic_request_projection() -> None:
    categories = {item.value: [] for item in ResearchInputCategory}
    prepared = PreparedResearchInput(
        profile_checksum="sha256:" + "a" * 64,
        selected_item_ids=("task",),
        context_group_refs=("context-group://task",),
        system_prompt_ref="prompt://system/v1",
        user_payload=categories,
        tools_ref="tools://research/v1",
        output_schema_ref="schema://answer/v1",
    )
    assert prepared.prepared_request_checksum.startswith("sha256:")

    with pytest.raises(ValueError, match="exactly the five"):
        PreparedResearchInput(
            profile_checksum="sha256:" + "a" * 64,
            selected_item_ids=("task",),
            context_group_refs=("context-group://task",),
            system_prompt_ref="prompt://system/v1",
            user_payload={"current_task": []},
            tools_ref="tools://research/v1",
            output_schema_ref="schema://answer/v1",
        )


def test_rollover_requires_durable_next_state_and_reconciled_tool_transactions() -> None:
    current = _manifest(window_sequence=0)
    candidate = _manifest(window_sequence=1, state_revision="state-2")

    assert validate_rollover(current, candidate) is ContextRolloverDecision.READY
    with pytest.raises(ValueError, match="tool transactions"):
        validate_rollover(current, candidate, pending_tool_transaction=True)
    with pytest.raises(ValueError, match="sequence"):
        validate_rollover(current, _manifest(window_sequence=2, state_revision="state-2"))
    with pytest.raises(ValueError, match="policy"):
        validate_rollover(current, _manifest(window_sequence=1, state_revision="state-2", policy_revision="policy-2"))

