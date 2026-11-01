"""report render/validate, quality.*, memory.* and control.* under faults."""

from __future__ import annotations

from typing import Any

import pytest

from framework.tool import ToolExecutor, ToolStatus
from framework.tool.builtin import control as control_tools
from framework.tool.builtin import memory as memory_tools
from framework.workers.approval.model import ApprovalDecision
from framework.workers.runtime.backpressure import BackpressurePolicy
from tests.tools.harness.fault_stores import Pass, Raise, RaiseAfter, Stall
from tests.tools.harness.invoke import expect_succeeded, run_tool
from tests.tools.harness.registry import ToolEnvironment, tool_test_identity
from tests.tools.test_notification_webhook_tools import outcome

NAN = float("nan")
REPORT = {
    "title": "Agent memory weekly",
    "sections": [
        {
            "title": "Findings",
            "content": "Agent memory retrieval improves long tasks.",
            "sources": ["https://example.com/memory"],
        }
    ],
    "source_urls": ["https://example.com/memory"],
}
EVIDENCE = {
    "items": [
        {
            "evidence_id": "e1",
            "source_url": "https://example.com/memory",
            "title": "Agent memory retrieval",
            "summary": "Agent memory retrieval improves long tasks.",
            "confidence": 0.8,
        }
    ]
}
UNUSABLE_REPORTS = [
    {"sections": []},
    {"title": "   ", "sections": []},
    {"title": 7},
    {"title": "t", "sections": "not a list"},
    {"title": "t", "sections": ["not an object"]},
    {"title": "t", "sections": [{"content": "no title"}]},
    {"title": "t", "sections": [{"title": "S", "content": {"nested": "object"}}]},
    {"title": "t", "source_urls": [None, 3]},
    {"title": "t", "source_urls": "https://example.com"},
    {"title": "t", "metadata": ["not", "an", "object"]},
    {"title": "t", "metadata": {"score": NAN}},
    {"title": "t", "metadata": {"score": float("inf")}},
]
UNUSABLE_REPORT_IDS = [
    "no-title",
    "blank-title",
    "number-title",
    "sections-string",
    "section-string",
    "section-untitled",
    "content-object",
    "source-urls-not-strings",
    "source-urls-string",
    "metadata-list",
    "metadata-nan",
    "metadata-inf",
]


# --- report.render_markdown / render_json / validate ---------------------------------


@pytest.mark.tool_scenario("report.render_markdown", "A")
@pytest.mark.tool_scenario("report.render_json", "A")
@pytest.mark.parametrize("tool", ["report.render_markdown", "report.render_json"])
@pytest.mark.parametrize("report", UNUSABLE_REPORTS, ids=UNUSABLE_REPORT_IDS)
def test_renderers_refuse_reports_the_writers_would_refuse(tool_env: ToolEnvironment, tool: str, report: dict) -> None:
    run = tool_env.run(tool, {"report": report})
    assert run.status is ToolStatus.FAILED, run.describe()
    assert run.observation.result.error_type != "AttributeError"


@pytest.mark.tool_scenario("report.validate", "A")
@pytest.mark.parametrize("report", UNUSABLE_REPORTS, ids=UNUSABLE_REPORT_IDS)
def test_validate_agrees_with_export_and_publish(tool_env: ToolEnvironment, report: dict) -> None:
    verdict = expect_succeeded(tool_env.run("report.validate", {"report": report}))
    export = tool_env.run("report.export", {"report": report})
    publish = tool_env.run("report.publish", {"report": report})

    assert verdict["valid"] is False and verdict["errors"]
    assert outcome(export) == "not_applied"
    assert outcome(publish) == "not_applied"


@pytest.mark.tool_scenario("report.validate", "A")
@pytest.mark.tool_scenario("report.render_json", "A")
def test_valid_report_is_normalized_the_same_way_everywhere(tool_env: ToolEnvironment) -> None:
    padded = {**REPORT, "title": "  Agent memory weekly  ", "source_urls": ["  https://example.com/memory "]}

    verdict = expect_succeeded(tool_env.run("report.validate", {"report": padded}))
    rendered = expect_succeeded(tool_env.run("report.render_json", {"report": padded}))["report"]
    markdown = expect_succeeded(tool_env.run("report.render_markdown", {"report": padded}))["markdown"]

    assert verdict == {"valid": True, "errors": [], "section_count": 1, "source_url_count": 1}
    assert rendered["title"] == "Agent memory weekly"
    assert rendered["source_urls"] == ["https://example.com/memory"]
    assert markdown.startswith("# Agent memory weekly\n")
    assert "- https://example.com/memory" in markdown


@pytest.mark.tool_scenario("report.render_markdown", "D")
@pytest.mark.tool_scenario("report.render_json", "D")
@pytest.mark.parametrize("tool", ["report.render_markdown", "report.render_json"])
def test_oversized_render_fails_cleanly_and_fast(tool_env: ToolEnvironment, tool: str) -> None:
    report = {"title": "Big", "sections": [{"title": f"S{index}", "content": "x " * 2000} for index in range(600)]}

    run = tool_env.run(tool, {"report": report})

    assert run.status is ToolStatus.FAILED
    assert "max_result_bytes" in run.error_message
    assert run.elapsed_seconds < 5.0


@pytest.mark.tool_scenario("report.validate", "D")
def test_validate_of_a_large_report_returns_counts_only(tool_env: ToolEnvironment) -> None:
    report = {"title": "Big", "sections": [{"title": f"S{index}", "content": "x " * 2000} for index in range(600)]}

    run = tool_env.run("report.validate", {"report": report})

    assert expect_succeeded(run)["section_count"] == 600
    assert run.observation.result.output_bytes < 1_000
    assert run.elapsed_seconds < 5.0


# --- quality.* -------------------------------------------------------------------------

QUALITY_TOOLS = ["quality.citation_check", "quality.claim_support_check", "quality.editor_score"]


@pytest.mark.tool_scenario("quality.citation_check", "A")
@pytest.mark.tool_scenario("quality.claim_support_check", "A")
@pytest.mark.tool_scenario("quality.editor_score", "A")
@pytest.mark.parametrize("tool", QUALITY_TOOLS)
@pytest.mark.parametrize(
    "report, evidence",
    [
        ({"title": "t", "sections": "abc"}, EVIDENCE),
        ({"title": "t", "sections": ["abc"]}, EVIDENCE),
        ({"title": "t", "sections": [{"title": "S", "content": ["a", "list"]}]}, EVIDENCE),
        (REPORT, {"items": "abc"}),
        (REPORT, {"items": ["abc"]}),
        (REPORT, {"items": [{"confidence": "high"}]}),
        (REPORT, {"items": [{"confidence": True}]}),
        (REPORT, {"items": [{"confidence": NAN}]}),
        (REPORT, {"items": [{"confidence": 1.5}]}),
        (REPORT, {"items": [{"metadata": [1, 2]}]}),
        (REPORT, {"items": [], "source_map": {"e1": "https://example.com"}}),
        (REPORT, {"items": [], "missing_information": "abc"}),
        (REPORT, {"items": [], "coverage_notes": [1, 2]}),
        (REPORT, {"items": [], "metadata": "abc"}),
    ],
    ids=[
        "sections-string",
        "section-string",
        "content-list",
        "items-string",
        "item-string",
        "confidence-text",
        "confidence-bool",
        "confidence-nan",
        "confidence-above-1",
        "item-metadata-list",
        "source-map-string",
        "missing-information-string",
        "coverage-notes-numbers",
        "bundle-metadata-string",
    ],
)
def test_quality_checks_reject_malformed_payloads_with_a_clear_reason(
    tool_env: ToolEnvironment, tool: str, report: dict, evidence: dict
) -> None:
    run = tool_env.run(tool, {"report": report, "evidence_bundle": evidence})

    assert run.status is ToolStatus.FAILED, run.describe()
    assert run.observation.result.error_type == "ValueError", run.describe()


@pytest.mark.tool_scenario("quality.citation_check", "A")
@pytest.mark.tool_scenario("quality.claim_support_check", "A")
@pytest.mark.tool_scenario("quality.editor_score", "A")
def test_supported_report_passes_every_quality_check(tool_env: ToolEnvironment) -> None:
    args = {"report": REPORT, "evidence_bundle": EVIDENCE}

    citation = expect_succeeded(tool_env.run("quality.citation_check", args))
    support = expect_succeeded(tool_env.run("quality.claim_support_check", args))
    score = expect_succeeded(tool_env.run("quality.editor_score", args))

    assert citation["unknown_urls"] == []
    assert support["unsupported_section_count"] == 0
    assert 0.0 <= score["quality_score"] <= 1.0


@pytest.mark.tool_scenario("quality.citation_check", "D")
@pytest.mark.tool_scenario("quality.claim_support_check", "D")
@pytest.mark.tool_scenario("quality.editor_score", "D")
@pytest.mark.parametrize("tool", QUALITY_TOOLS)
def test_quality_checks_on_large_inputs_finish_in_bounded_time(tool_env: ToolEnvironment, tool: str) -> None:
    report = {
        "title": "Big",
        "sections": [
            {"title": f"S{index}", "content": "agent memory retrieval " * 100, "sources": [f"https://a.com/{index}"]}
            for index in range(300)
        ],
    }
    evidence = {
        "items": [
            {
                "evidence_id": f"e{index}",
                "source_url": f"https://a.com/{index}",
                "title": "memory",
                "summary": "agent memory retrieval " * 20,
                "confidence": 0.5,
            }
            for index in range(2000)
        ]
    }

    run = tool_env.run(tool, {"report": report, "evidence_bundle": evidence})

    assert run.status is ToolStatus.SUCCEEDED, run.describe()
    assert run.elapsed_seconds < 15.0


@pytest.mark.tool_scenario("quality.duplicate_check", "A")
@pytest.mark.parametrize("items", [[1, 2], ["https://a.com"], [None]], ids=["numbers", "strings", "null"])
def test_duplicate_check_rejects_non_object_items(tool_env: ToolEnvironment, items: list) -> None:
    run = tool_env.run("quality.duplicate_check", {"items": items})
    assert run.status is ToolStatus.FAILED
    assert "must be an object" in run.error_message


@pytest.mark.tool_scenario("quality.duplicate_check", "A")
def test_one_malformed_url_does_not_abort_the_duplicate_check(tool_env: ToolEnvironment) -> None:
    items = [
        {"item_id": "a", "url": "http://[::1"},
        {"item_id": "b", "url": "http://[::1"},
        {"item_id": "c", "url": "https://Example.com/post?utm_source=x"},
        {"item_id": "d", "url": "https://example.com/post"},
        {"item_id": "e", "url": "https://other.com/"},
    ]

    groups = expect_succeeded(tool_env.run("quality.duplicate_check", {"items": items}))["duplicate_groups"]

    by_ids = {tuple(group["item_ids"]): group["reasons"] for group in groups}
    assert by_ids[("a", "b")] == ["raw_url"]
    assert ("c", "d") in by_ids
    assert all("e" not in ids for ids in by_ids)


@pytest.mark.tool_scenario("quality.duplicate_check", "D")
def test_duplicate_check_over_many_items_is_near_linear(tool_env: ToolEnvironment) -> None:
    items = [{"item_id": f"i{index}", "url": f"https://ex.com/{index % 50}", "title": f"t{index}"} for index in range(20_000)]

    run = tool_env.run("quality.duplicate_check", {"items": items})

    output = expect_succeeded(run)
    assert output["duplicate_group_count"] == 50
    assert output["duplicate_item_count"] == 19_950
    assert run.elapsed_seconds < 10.0


# --- memory.recall / memory.explain ----------------------------------------------------


@pytest.mark.tool_scenario("memory.recall", "A")
@pytest.mark.parametrize(
    "args",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "q", "min_score": NAN},
        {"query": "q", "score_threshold": float("inf")},
        {"query": "q", "max_context_tokens": 0},
        {"query": "q", "filters": ["not", "an", "object"]},
        {"query": "q", "limit": "many"},
    ],
    ids=["empty", "blank", "min-score-nan", "threshold-inf", "zero-tokens", "filters-list", "limit-text"],
)
def test_recall_rejects_unusable_queries_before_reading(tool_env: ToolEnvironment, args: dict) -> None:
    assert tool_env.run("memory.recall", args).status is ToolStatus.FAILED
    assert tool_env.memory.plan.calls("recall") == 0


@pytest.mark.tool_scenario("memory.recall", "B")
def test_recall_store_failure_is_a_clean_read_failure(tool_env: ToolEnvironment) -> None:
    tool_env.memory.plan.script("recall", Raise(ConnectionError("memory store down")))

    run = tool_env.run("memory.recall", {"query": "agent memory"})

    assert run.status is ToolStatus.FAILED
    assert not run.observation.result.indeterminate


@pytest.mark.tool_scenario("memory.recall", "C")
def test_recall_stall_ends_on_the_tool_timeout(tool_env_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_tools, "MEMORY_RECALL_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.memory.plan.script("recall", Stall())
    try:
        run = environment.run("memory.recall", {"query": "agent memory"})
        assert run.status is ToolStatus.TIMEOUT, run.describe()
        assert run.elapsed_seconds < 5.0
    finally:
        environment.memory.plan.release()


@pytest.mark.tool_scenario("memory.recall", "E")
def test_parallel_recalls_overlap(tool_env: ToolEnvironment) -> None:
    tool_env.memory.plan.script("recall", Stall(seconds=0.2))

    observations, elapsed = tool_env.run_batch([("memory.recall", {"query": f"q{index}"}) for index in range(10)])

    assert all(observation.status is ToolStatus.SUCCEEDED for observation in observations)
    assert elapsed < 1.2


@pytest.mark.tool_scenario("memory.recall", "G")
@pytest.mark.parametrize(
    "args",
    [
        {"query": "q", "filters": {"run_id": "run-someone-else"}},
        {"query": "q", "filters": {"graph_id": "another-graph"}},
        {"query": "q", "collection": "another-tenant"},
    ],
    ids=["foreign-run", "foreign-graph", "foreign-collection"],
)
def test_recall_cannot_widen_its_namespace(tool_env: ToolEnvironment, args: dict) -> None:
    assert tool_env.run("memory.recall", args).status is ToolStatus.FAILED
    assert tool_env.memory.plan.calls("recall") == 0


@pytest.mark.tool_scenario("memory.recall", "G")
def test_recall_from_another_graph_activity_is_refused(tool_env: ToolEnvironment) -> None:
    other = ToolExecutor(tool_env.registry, graph_identity=tool_test_identity("run-someone-else"))

    run = run_tool(tool_env.registry, "memory.recall", {"query": "q"}, executor=other)

    assert run.status is not ToolStatus.SUCCEEDED
    assert tool_env.memory.plan.calls("recall") == 0


@pytest.mark.tool_scenario("memory.explain", "A")
def test_explain_takes_no_arguments(tool_env: ToolEnvironment) -> None:
    assert tool_env.run("memory.explain", {"verbose": True}).status is ToolStatus.FAILED
    assert expect_succeeded(tool_env.run("memory.explain", {}))["tools"]["memory.recall"] == "available"


@pytest.mark.tool_scenario("memory.explain", "D")
def test_explain_is_small_and_does_not_touch_the_store(tool_env: ToolEnvironment) -> None:
    run = tool_env.run("memory.explain", {})

    assert run.observation.result.output_bytes < 2_000
    assert tool_env.memory.plan.calls("recall") == 0


# --- control.set_output / report_progress ----------------------------------------------


@pytest.mark.tool_scenario("control.set_output", "A")
@pytest.mark.parametrize(
    "args",
    [{"output": {"score": NAN}}, {"output": {"nested": [float("inf")]}}, {"output": "text"}],
    ids=["nan", "nested-inf", "not-object"],
)
def test_set_output_refuses_outputs_that_cannot_be_stored(tool_env: ToolEnvironment, args: dict) -> None:
    assert tool_env.run("control.set_output", args).status is ToolStatus.FAILED


@pytest.mark.tool_scenario("control.set_output", "D")
def test_large_output_is_accepted_without_inline_failure(tool_env: ToolEnvironment) -> None:
    output = {"items": [{"index": index, "text": "x" * 100} for index in range(2_000)]}

    run = tool_env.run("control.set_output", {"output": output})

    assert expect_succeeded(run)["output"]["items"][-1]["index"] == 1_999
    assert run.elapsed_seconds < 5.0


@pytest.mark.tool_scenario("control.report_progress", "A")
@pytest.mark.parametrize(
    "args",
    [
        {"message": "m", "percent": NAN},
        {"message": "m", "percent": float("inf")},
        {"message": "m", "percent": -1},
        {"message": "m", "percent": 101},
        {"message": "m", "percent": "50"},
        {"message": "m", "metadata": {"eta": NAN}},
        {"message": "m", "metadata": ["list"]},
    ],
    ids=["percent-nan", "percent-inf", "percent-negative", "percent-above-100", "percent-text", "metadata-nan", "metadata-list"],
)
def test_report_progress_rejects_unusable_progress(tool_env: ToolEnvironment, args: dict) -> None:
    assert tool_env.run("control.report_progress", args).status is ToolStatus.FAILED


@pytest.mark.tool_scenario("control.report_progress", "D")
def test_parallel_progress_reports_are_independent(tool_env: ToolEnvironment) -> None:
    observations, elapsed = tool_env.run_batch(
        [("control.report_progress", {"message": f"step {index}", "percent": index}) for index in range(50)]
    )

    assert [observation.result.output["percent"] for observation in observations] == [float(index) for index in range(50)]
    assert elapsed < 5.0


# --- control.request_human_review / escalate -------------------------------------------

APPROVAL_TOOLS = {
    "control.request_human_review": {"requested_action": "publish", "reason": "check citations"},
    "control.escalate": {"escalation_type": "source_blocked", "reason": "source returns 403"},
}


def _approvals(environment: ToolEnvironment) -> list[Any]:
    return environment.approvals.inner.list_approvals()


@pytest.mark.tool_scenario("control.request_human_review", "A")
@pytest.mark.tool_scenario("control.escalate", "A")
@pytest.mark.parametrize("tool", list(APPROVAL_TOOLS))
@pytest.mark.parametrize(
    "extra",
    [
        {"reason": "  "},
        {"payload": {"api_key": "sk-live"}},
        {"metadata": {"password": "hunter2"}},
        {"payload": {"score": NAN}},
        {"payload": ["list"]},
    ],
    ids=["blank-reason", "secret-payload", "secret-metadata", "nan-payload", "payload-list"],
)
def test_approval_requests_with_unusable_fields_are_not_stored(tool_env: ToolEnvironment, tool: str, extra: dict) -> None:
    run = tool_env.run(tool, {**APPROVAL_TOOLS[tool], **extra})

    assert outcome(run) == "not_applied"
    assert tool_env.approvals.plan.calls("upsert_approval") == 0


@pytest.mark.tool_scenario("control.request_human_review", "A")
@pytest.mark.tool_scenario("control.escalate", "A")
@pytest.mark.parametrize(
    "tool, extra",
    [("control.request_human_review", {"risk_level": "whatever"}), ("control.escalate", {"severity": "bogus"})],
    ids=["review-risk", "escalate-severity"],
)
def test_unknown_risk_levels_are_rejected(tool_env: ToolEnvironment, tool: str, extra: dict) -> None:
    assert tool_env.run(tool, {**APPROVAL_TOOLS[tool], **extra}).status is ToolStatus.FAILED
    assert _approvals(tool_env) == []


@pytest.mark.tool_scenario("control.request_human_review", "B")
@pytest.mark.tool_scenario("control.escalate", "B")
@pytest.mark.parametrize("tool", list(APPROVAL_TOOLS))
def test_approval_store_failure_leaves_nothing_and_retry_creates_one(tool_env: ToolEnvironment, tool: str) -> None:
    tool_env.approvals.plan.script("upsert_approval", Raise(ConnectionError("approval store down")), Pass())

    assert tool_env.run(tool, APPROVAL_TOOLS[tool]).status is ToolStatus.FAILED
    assert _approvals(tool_env) == []
    expect_succeeded(tool_env.run(tool, APPROVAL_TOOLS[tool]))
    assert len(_approvals(tool_env)) == 1


@pytest.mark.tool_scenario("control.request_human_review", "C")
@pytest.mark.tool_scenario("control.escalate", "C")
@pytest.mark.parametrize("tool", list(APPROVAL_TOOLS))
def test_approval_store_stall_ends_on_the_tool_timeout(tool_env_factory, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    monkeypatch.setattr(control_tools, "CONTROL_STORE_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.approvals.plan.script("upsert_approval", Stall())
    try:
        run = environment.run(tool, APPROVAL_TOOLS[tool])
        assert run.status is ToolStatus.TIMEOUT, run.describe()
        assert run.elapsed_seconds < 5.0
    finally:
        environment.approvals.plan.release()


@pytest.mark.tool_scenario("control.request_human_review", "F")
@pytest.mark.tool_scenario("control.escalate", "F")
@pytest.mark.parametrize("tool", list(APPROVAL_TOOLS))
def test_retry_after_a_lost_acknowledgement_reuses_the_request(tool_env: ToolEnvironment, tool: str) -> None:
    tool_env.approvals.plan.script("upsert_approval", RaiseAfter(TimeoutError("ack lost")), Pass())

    assert tool_env.run(tool, APPROVAL_TOOLS[tool]).status is ToolStatus.FAILED
    retried = expect_succeeded(tool_env.run(tool, APPROVAL_TOOLS[tool]))

    assert retried["existing"] is True
    assert [approval.approval_id for approval in _approvals(tool_env)] == [retried["approval_id"]]


@pytest.mark.tool_scenario("control.request_human_review", "F")
@pytest.mark.tool_scenario("control.escalate", "F")
@pytest.mark.parametrize("tool", list(APPROVAL_TOOLS))
def test_repeating_a_decided_request_does_not_reset_the_decision(tool_env: ToolEnvironment, tool: str) -> None:
    first = expect_succeeded(tool_env.run(tool, APPROVAL_TOOLS[tool]))
    tool_env.approvals.inner.record_decision(
        first["approval_id"], decision=ApprovalDecision(decision_type="approve", decided_by="editor")
    )

    repeated = expect_succeeded(tool_env.run(tool, APPROVAL_TOOLS[tool]))

    assert repeated["existing"] is True
    assert repeated["approval"]["status"] == "approved"
    assert len(_approvals(tool_env)) == 1


@pytest.mark.tool_scenario("control.request_human_review", "F")
def test_different_requests_get_different_approvals(tool_env: ToolEnvironment) -> None:
    first = expect_succeeded(tool_env.run("control.request_human_review", {"requested_action": "publish", "reason": "a"}))
    second = expect_succeeded(tool_env.run("control.request_human_review", {"requested_action": "publish", "reason": "b"}))

    assert first["approval_id"] != second["approval_id"]
    assert len(_approvals(tool_env)) == 2


# --- control.delegate_to_subagent ------------------------------------------------------

DELEGATION = {"task_type": "summarize", "payload": {"report_id": "r1"}}


def _queued(environment: ToolEnvironment) -> int:
    return environment.tasks.inner.queue_status("framework:queue:default").pending_count


@pytest.mark.tool_scenario("control.delegate_to_subagent", "A")
@pytest.mark.parametrize(
    "extra",
    [
        {"task_type": "  "},
        {"payload": {"token": "secret"}},
        {"payload": {"score": NAN}},
        {"metadata": {"eta": float("inf")}},
        {"max_attempts": 0},
        {"max_attempts": 1_000},
        {"queue_name": "  "},
    ],
    ids=["blank-type", "secret-payload", "nan-payload", "inf-metadata", "zero-attempts", "too-many-attempts", "blank-queue"],
)
def test_unusable_delegations_are_not_enqueued(tool_env: ToolEnvironment, extra: dict) -> None:
    run = tool_env.run("control.delegate_to_subagent", {**DELEGATION, **extra})

    assert outcome(run) == "not_applied"
    assert tool_env.tasks.plan.calls("enqueue") == 0


@pytest.mark.tool_scenario("control.delegate_to_subagent", "B")
def test_queue_failure_is_indeterminate(tool_env: ToolEnvironment) -> None:
    tool_env.tasks.plan.script("enqueue", Raise(ConnectionError("queue down")))
    assert outcome(tool_env.run("control.delegate_to_subagent", DELEGATION)) == "indeterminate"


@pytest.mark.tool_scenario("control.delegate_to_subagent", "B")
def test_queue_refusal_is_reported_as_not_applied(tool_env: ToolEnvironment) -> None:
    tool_env.tasks.inner.backpressure_policy = BackpressurePolicy(max_pending_per_queue=0)

    run = tool_env.run("control.delegate_to_subagent", DELEGATION)

    assert outcome(run) == "not_applied"
    assert "backpressure" in run.error_message
    assert _queued(tool_env) == 0


@pytest.mark.tool_scenario("control.delegate_to_subagent", "C")
def test_queue_stall_ends_on_the_tool_timeout(tool_env_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control_tools, "CONTROL_STORE_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.tasks.plan.script("enqueue", Stall())
    try:
        run = environment.run("control.delegate_to_subagent", DELEGATION)
        assert run.status is ToolStatus.TIMEOUT, run.describe()
    finally:
        environment.tasks.plan.release()


@pytest.mark.tool_scenario("control.delegate_to_subagent", "F")
def test_retry_after_a_lost_acknowledgement_queues_the_work_once(tool_env: ToolEnvironment) -> None:
    tool_env.tasks.plan.script("enqueue", RaiseAfter(TimeoutError("ack lost")), Pass())

    assert outcome(tool_env.run("control.delegate_to_subagent", DELEGATION)) == "indeterminate"
    retried = expect_succeeded(tool_env.run("control.delegate_to_subagent", DELEGATION))

    assert retried["deduplicated"] is True
    assert _queued(tool_env) == 1


@pytest.mark.tool_scenario("control.delegate_to_subagent", "F")
def test_different_work_is_queued_separately(tool_env: ToolEnvironment) -> None:
    first = expect_succeeded(tool_env.run("control.delegate_to_subagent", DELEGATION))
    second = expect_succeeded(tool_env.run("control.delegate_to_subagent", {**DELEGATION, "payload": {"report_id": "r2"}}))

    assert first["dedup_key"] != second["dedup_key"]
    assert _queued(tool_env) == 2
