from __future__ import annotations

from dataclasses import replace
import json

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
        source_revision=("task-revision-1" if category is ResearchInputCategory.CURRENT_TASK else "snapshot-1"),
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



def test_input_metadata_is_deeply_immutable_and_checksum_bound() -> None:
    metadata = {"reader": {"conditions": ["test split"], "revision": "v1"}}
    task = replace(
        _item("task", ResearchInputCategory.CURRENT_TASK, "Explain the method"),
        metadata=metadata,
    )
    profile = _profile(task)
    original_checksum = profile.checksum
    serialized = profile.to_dict()

    metadata["reader"]["conditions"].append("training split")
    metadata["reader"]["revision"] = "v2"
    assert task.metadata["reader"]["conditions"] == ("test split",)
    assert task.metadata["reader"]["revision"] == "v1"
    assert profile.checksum == original_checksum
    assert profile.to_dict() == serialized
    with pytest.raises(TypeError):
        task.metadata["reader"]["revision"] = "v3"
    with pytest.raises(AttributeError):
        task.metadata["reader"]["conditions"].append("forged")

    restored = ResearchInputProfile.from_dict(json.loads(json.dumps(serialized)))
    assert restored.to_dict() == serialized
    serialized["items"][0]["metadata"]["reader"]["conditions"].append("forged")
    with pytest.raises(ValueError, match="checksum mismatch"):
        ResearchInputProfile.from_dict(serialized)


def test_current_task_revision_and_uniqueness_are_authoritative() -> None:
    task = _item("task", ResearchInputCategory.CURRENT_TASK, "Explain the method")
    with pytest.raises(ValueError, match="task_revision must match"):
        _profile(replace(task, source_revision="obsolete-task-revision"))
    with pytest.raises(ValueError, match="exactly one current_task"):
        _profile(_item("paper", ResearchInputCategory.PAPER_MATERIAL, "source://paper"))
    with pytest.raises(ValueError, match="exactly one current_task"):
        _profile(task, replace(task, item_id="duplicate-task"))


@pytest.mark.parametrize("category", list(ResearchInputCategory))
@pytest.mark.parametrize("status", list(ResearchInputStatus))
def test_category_status_matrix_prevents_trust_upgrade(category, status) -> None:
    allowed = {
        ResearchInputCategory.CURRENT_TASK: {ResearchInputStatus.RAW},
        ResearchInputCategory.PAPER_MATERIAL: {
            ResearchInputStatus.RAW, ResearchInputStatus.AUTHOR_CLAIM, ResearchInputStatus.UNAVAILABLE,
        },
        ResearchInputCategory.EXISTING_CONCLUSION: set(ResearchInputStatus),
        ResearchInputCategory.TOOL_OBSERVATION: {
            ResearchInputStatus.RAW, ResearchInputStatus.MODEL_CANDIDATE, ResearchInputStatus.UNAVAILABLE,
        },
        ResearchInputCategory.NECESSARY_HISTORY: {
            ResearchInputStatus.RAW, ResearchInputStatus.AUTHOR_CLAIM,
            ResearchInputStatus.MODEL_CANDIDATE, ResearchInputStatus.UNAVAILABLE,
        },
    }
    qualification_ref = "qualification://trusted" if status is ResearchInputStatus.VERIFIED_CONCLUSION else None
    if status in allowed[category]:
        assert _item("item", category, "content://item", status=status, qualification_ref=qualification_ref).status is status
    else:
        with pytest.raises(ValueError, match="status is not valid"):
            _item("item", category, "content://item", status=status, qualification_ref=qualification_ref)


@pytest.mark.parametrize("constraints", [{"use source": True}, {"use source"}, "use source", None])
def test_explicit_profile_constraints_require_an_ordered_sequence(constraints) -> None:
    profile = _profile(_item("task", ResearchInputCategory.CURRENT_TASK, "Explain the method")).to_dict()
    profile["constraints"] = constraints
    with pytest.raises(TypeError, match="constraints must be a sequence"):
        ResearchInputProfile.from_dict(profile)


def test_prepared_input_round_trip_detects_nested_payload_tampering() -> None:
    payload = {category.value: [] for category in ResearchInputCategory}
    payload["current_task"] = [{"conditions": ["test split"]}]
    prepared = PreparedResearchInput(
        profile_checksum="sha256:" + "a" * 64, selected_item_ids=("task",),
        context_group_refs=("context://task",), system_prompt_ref="prompt://system/v1",
        user_payload=payload, tools_ref="tools://research/v1", output_schema_ref="schema://answer/v1",
    )
    payload["current_task"][0]["conditions"].append("untrusted mutation")
    assert prepared.user_payload["current_task"][0]["conditions"] == ("test split",)
    serialized = json.loads(json.dumps(prepared.to_dict()))
    assert PreparedResearchInput.from_dict(serialized).to_dict() == prepared.to_dict()
    with pytest.raises(ValueError, match="schema mismatch"):
        PreparedResearchInput.from_dict({**serialized, "schema": "newsroom.research-input-contract/v999"})
    serialized["user_payload"]["current_task"][0]["conditions"].append("forged")
    with pytest.raises(ValueError, match="checksum mismatch"):
        PreparedResearchInput.from_dict(serialized)
