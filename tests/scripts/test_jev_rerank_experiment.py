from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from scripts import jev_rerank_experiment as experiment
from scripts.jev_rerank_dataset import DATASET_SCHEMA, manifest_fingerprint


def _manifest(*, case_count: int = 2, candidate_count: int = 3) -> dict[str, Any]:
    cases = []
    for case_index in range(case_count):
        candidates = [
            {
                "id": f"chunk_{case_index}_{candidate_index}",
                "text": f"Case {case_index} candidate {candidate_index} exact text.",
                "paper_id": f"paper_{candidate_index}",
                "source_ref": f"arxiv://paper_{candidate_index}/pdf#page=1",
            }
            for candidate_index in range(candidate_count)
        ]
        cases.append(
            {
                "id": f"case_{case_index}",
                "paper_id": f"paper_{case_index}",
                "query": f"Question {case_index}?",
                "gold_ids": [candidates[-1]["id"]],
                "candidates": candidates,
            }
        )
    return {
        "schema": DATASET_SCHEMA,
        "metadata": {"seed": "unit-test", "counts": {"abstain_questions_excluded": 12}},
        "cases": cases,
    }


def _write_manifest(tmp_path: Path, payload: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(payload or _manifest(), ensure_ascii=False), encoding="utf-8"
    )
    return path


def _jev_response(request: dict[str, Any], *, score: float = 3.0) -> dict[str, Any]:
    probabilities = {
        "0": float(score == 0),
        "1": float(score == 1),
        "2": float(score == 2),
        "3": float(score == 3),
    }
    legend = {
        str(index): text for index, text in enumerate(experiment.JEV_SCORE_CRITERIA)
    }
    return {
        "model": request["model"],
        "answers": {
            question_id: {
                "type": "score",
                "score": score,
                "confidence": 1.0,
                "legend": legend,
                "probabilities": probabilities,
            }
            for question_id in request["questions"]
        },
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


def _arm_report(
    manifest: dict[str, Any], arm: str, cases: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    candidate_orders = {
        case["id"]: [candidate["id"] for candidate in case["candidates"]]
        for case in manifest["cases"]
    }
    report_cases = cases
    if report_cases is None:
        report_cases = [
            {
                "id": case["id"],
                "status": "failed",
                "gold_ids": case["gold_ids"],
                "candidate_ids": [item["id"] for item in case["candidates"]],
                "ranking": None,
            }
            for case in manifest["cases"]
        ]
    return {
        "schema": experiment.ARM_SCHEMA,
        "arm": arm,
        "status": "complete",
        "dataset": {
            "fingerprint": manifest_fingerprint(manifest),
            "case_ids": [case["id"] for case in manifest["cases"]],
            "candidate_order_fingerprint": experiment.sha256_json(candidate_orders),
            "metadata": manifest["metadata"],
            "coverage": experiment.dataset_coverage(manifest),
        },
        "cases": report_cases,
        "metrics": {},
        "usage": {},
        "warmup": {},
        "cold_load": {},
        "failures": [],
        "config": {},
        "config_fingerprint": experiment.sha256_json({}),
    }


def _write_report(tmp_path: Path, report: dict[str, Any], name: str) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_stable_ranking_preserves_original_order_for_ties() -> None:
    case = _manifest(case_count=1)["cases"][0]

    ranking = experiment.stable_ranking(case, [0.5, 0.9, 0.5])

    assert [item["id"] for item in ranking] == ["chunk_0_1", "chunk_0_0", "chunk_0_2"]
    assert [item["original_index"] for item in ranking] == [1, 0, 2]


def test_build_jev_payload_preserves_text_and_never_sends_gold() -> None:
    case = _manifest(case_count=1)["cases"][0]

    payload, question_ids = experiment.build_jev_payload(
        case, case["candidates"], model=experiment.DEFAULT_JEV_MODEL
    )

    assert payload["state"]["query"] == case["query"]
    assert payload["state"]["passages"] == [
        {"id": item["id"], "text": item["text"]} for item in case["candidates"]
    ]
    assert "gold" not in json.dumps(payload).lower()
    assert question_ids == ("passage_000", "passage_001", "passage_002")
    for candidate, question in zip(
        case["candidates"], payload["questions"].values(), strict=True
    ):
        assert candidate["id"] in question["instructions"]
        assert question["criteria"] == list(experiment.JEV_SCORE_CRITERIA)


def test_validate_jev_response_preserves_raw_fields_and_normalizes_score() -> None:
    request, question_ids = experiment.build_jev_payload(
        _manifest(case_count=1)["cases"][0],
        _manifest(case_count=1)["cases"][0]["candidates"][:1],
        model=experiment.DEFAULT_JEV_MODEL,
    )

    scores, answers, usage, model = experiment.validate_jev_response(
        _jev_response(request, score=2.0),
        expected_model=experiment.DEFAULT_JEV_MODEL,
        question_ids=question_ids,
    )

    assert scores == pytest.approx((2 / 3,))
    assert answers[0]["score"] == 2.0
    assert answers[0]["normalized_score"] == pytest.approx(2 / 3)
    assert answers[0]["confidence"] == 1.0
    assert answers[0]["probabilities"] == {
        "0": 0.0,
        "1": 0.0,
        "2": 1.0,
        "3": 0.0,
    }
    assert usage == {"input_tokens": 100, "output_tokens": 10}
    assert model == experiment.DEFAULT_JEV_MODEL


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda response: response.update(model="jev-latest"), "model mismatch"),
        (lambda response: response["answers"].pop("passage_000"), "answer IDs mismatch"),
        (
            lambda response: response["answers"]["passage_000"].update(score=math.nan),
            "must be finite",
        ),
        (
            lambda response: response["answers"]["passage_000"]["probabilities"].update(
                {"3": 2.0}
            ),
            "must be finite and in",
        ),
        (lambda response: response["usage"].update(input_tokens=-1), "non-negative"),
    ],
)
def test_validate_jev_response_rejects_invalid_provider_data(mutate, message: str) -> None:
    case = _manifest(case_count=1)["cases"][0]
    request, question_ids = experiment.build_jev_payload(
        case, case["candidates"][:1], model=experiment.DEFAULT_JEV_MODEL
    )
    response = _jev_response(request)
    mutate(response)

    with pytest.raises(experiment.ProviderResponseError, match=message):
        experiment.validate_jev_response(
            response,
            expected_model=experiment.DEFAULT_JEV_MODEL,
            question_ids=question_ids,
        )


def test_missing_jev_key_is_blocked_and_does_not_call_provider(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    called = False

    def sender(*_args):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    report = experiment.run_arm(
        manifest_path, arm="jev", environ={}, sender=sender
    )

    assert called is False
    assert report["status"] == "blocked"
    assert report["blocked_reason"] == "missing_TYPESAFE_API_KEY"
    assert report["metrics"]["completion"] == {
        "total_cases": 2,
        "completed_cases": 0,
        "failed_cases": 0,
        "unmeasured_cases": 2,
        "completion_rate": 0.0,
    }
    assert report["metrics"]["quality"]["mrr"] is None


def test_jev_run_batches_identical_inputs_and_counts_warmup_usage_and_cost(
    tmp_path: Path,
) -> None:
    manifest = _manifest(case_count=2, candidate_count=3)
    manifest_path = _write_manifest(tmp_path, manifest)
    requests: list[dict[str, Any]] = []

    def sender(_endpoint, payload, api_key, timeout):
        assert api_key == "test-secret"
        assert timeout == 5.0
        requests.append(payload)
        return _jev_response(payload)

    report = experiment.run_arm(
        manifest_path,
        arm="jev",
        batch_size=2,
        timeout=5.0,
        max_calls=6,
        input_price_per_million=0.5,
        environ={"TYPESAFE_API_KEY": "test-secret"},
        sender=sender,
    )

    assert report["status"] == "complete"
    assert len(requests) == 6  # two warmup batches plus two batches per case
    assert report["warmup"]["calls"] == 2
    assert report["usage"] == {
        "calls": 6,
        "calls_with_known_usage": 6,
        "calls_with_unknown_usage": 0,
        "input_tokens": 600,
        "output_tokens": 60,
        "estimated_input_cost": pytest.approx(0.0003),
        "known_input_cost_lower_bound": None,
        "currency": "USD",
        "pricing": "explicit_cli_estimate",
    }
    assert "test-secret" not in json.dumps(report)
    first_measured_payloads = requests[2:4]
    assert [passage for request in first_measured_payloads for passage in request["state"]["passages"]] == [
        {"id": candidate["id"], "text": candidate["text"]}
        for candidate in manifest["cases"][0]["candidates"]
    ]
    assert report["cases"][0]["candidate_ids"] == [
        candidate["id"] for candidate in manifest["cases"][0]["candidates"]
    ]


def test_jev_partial_batch_failure_preserves_observation_without_fallback(
    tmp_path: Path,
) -> None:
    manifest_path = _write_manifest(tmp_path, _manifest(case_count=1, candidate_count=3))
    call_count = 0

    def sender(_endpoint, payload, _api_key, _timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 4:
            raise experiment.ProviderRequestError("http_529", "HTTP 529")
        return _jev_response(payload)

    report = experiment.run_arm(
        manifest_path,
        arm="jev",
        batch_size=2,
        max_calls=4,
        input_price_per_million=0.5,
        environ={"TYPESAFE_API_KEY": "secret"},
        sender=sender,
    )

    assert report["status"] == "failed"
    assert report["usage"]["calls"] == 4
    assert report["usage"]["calls_with_known_usage"] == 3
    assert report["usage"]["calls_with_unknown_usage"] == 1
    assert report["usage"]["input_tokens"] == 300
    assert report["usage"]["estimated_input_cost"] is None
    assert report["usage"]["known_input_cost_lower_bound"] == pytest.approx(0.00015)
    assert report["usage"]["pricing"] == "unavailable_incomplete_usage"
    assert report["cases"][0]["status"] == "failed"
    assert report["cases"][0]["ranking"] is None
    assert len(report["cases"][0]["observations"]) == 1
    assert report["cases"][0]["observations"][0]["candidate_ids"] == [
        "chunk_0_0",
        "chunk_0_1",
    ]
    assert report["failures"][-1]["code"] == "http_529"


def test_jev_counts_usage_when_answer_validation_fails(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, _manifest(case_count=1, candidate_count=1))
    call_count = 0

    def sender(_endpoint, payload, _api_key, _timeout):
        nonlocal call_count
        call_count += 1
        response = _jev_response(payload)
        if call_count == 2:
            response["answers"].clear()
        return response

    report = experiment.run_arm(
        manifest_path,
        arm="jev",
        batch_size=1,
        max_calls=2,
        input_price_per_million=0.5,
        environ={"TYPESAFE_API_KEY": "secret"},
        sender=sender,
    )

    assert report["status"] == "failed"
    assert report["usage"]["calls"] == 2
    assert report["usage"]["calls_with_known_usage"] == 2
    assert report["usage"]["calls_with_unknown_usage"] == 0
    assert report["usage"]["input_tokens"] == 200
    assert report["usage"]["estimated_input_cost"] == pytest.approx(0.0001)
    assert report["failures"][-1]["code"] == "ProviderResponseError"


def test_jev_rejects_insufficient_call_budget_before_provider_call(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, _manifest(case_count=2, candidate_count=3))
    calls = 0

    def sender(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError

    with pytest.raises(experiment.ConfigurationError, match="requires 6 calls"):
        experiment.run_arm(
            manifest_path,
            arm="jev",
            batch_size=2,
            max_calls=5,
            environ={"TYPESAFE_API_KEY": "secret"},
            sender=sender,
        )
    assert calls == 0


def test_quality_metrics_match_known_binary_ranking_values() -> None:
    cases = [
        {
            "status": "complete",
            "gold_ids": ["gold"],
            "ranking": [{"id": "other"}, {"id": "gold"}],
        },
        {
            "status": "complete",
            "gold_ids": ["missing"],
            "ranking": [{"id": "other"}, {"id": "gold"}],
        },
        {"status": "failed", "gold_ids": ["gold"], "ranking": None},
    ]

    metrics = experiment.quality_metrics(cases)

    assert metrics["evaluated_cases"] == 2
    assert metrics["hit_at_1"] == 0.0
    assert metrics["hit_at_3"] == 0.5
    assert metrics["hit_at_5"] == 0.5
    assert metrics["mrr"] == 0.25
    assert metrics["binary_ndcg_at_5"] == pytest.approx(0.5 / math.log2(3))


def test_summarize_rejects_dataset_fingerprint_mismatch(tmp_path: Path) -> None:
    first_manifest = _manifest()
    second_manifest = _manifest()
    second_manifest["metadata"]["seed"] = "different"
    lexical = _write_report(
        tmp_path, _arm_report(first_manifest, "lexical"), "lexical.json"
    )
    bge = _write_report(tmp_path, _arm_report(second_manifest, "bge"), "bge.json")

    with pytest.raises(experiment.ReportValidationError, match="fingerprint mismatch"):
        experiment.summarize_reports([lexical, bge])


def test_summarize_rejects_candidate_order_tampering(tmp_path: Path) -> None:
    manifest = _manifest(case_count=1)
    report = _arm_report(manifest, "lexical")
    report["cases"][0]["candidate_ids"].reverse()
    path = _write_report(tmp_path, report, "lexical.json")

    with pytest.raises(
        experiment.ReportValidationError, match="candidate order fingerprint"
    ):
        experiment.summarize_reports([path])


def test_summarize_marks_missing_arm_unmeasured_and_has_no_winner(tmp_path: Path) -> None:
    manifest = _manifest(case_count=1)
    case = manifest["cases"][0]
    completed = {
        "id": case["id"],
        "status": "complete",
        "gold_ids": case["gold_ids"],
        "candidate_ids": [item["id"] for item in case["candidates"]],
        "ranking": experiment.stable_ranking(case, [0.0, 0.0, 1.0]),
        "latency_ms": 1.0,
    }
    lexical = _write_report(
        tmp_path,
        _arm_report(manifest, "lexical", [completed]),
        "lexical.json",
    )
    bge = _write_report(
        tmp_path,
        _arm_report(manifest, "bge", [completed]),
        "bge.json",
    )

    summary = experiment.summarize_reports([lexical, bge])

    assert summary["arms"]["jev"] == {
        "status": "unmeasured",
        "reason": "arm report was not supplied",
        "metrics": None,
        "usage": None,
        "warmup": None,
        "cold_load": None,
    }
    assert summary["winner"] is None
    assert "jev" in summary["winner_reason"]
    assert summary["paired_comparisons"][0]["paired_case_count"] == 1


def test_write_json_refuses_to_overwrite_evidence(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    experiment.write_json(output, {"first": True})

    with pytest.raises(experiment.ConfigurationError, match="overwrite"):
        experiment.write_json(output, {"second": True})

    assert json.loads(output.read_text(encoding="utf-8")) == {"first": True}


def test_main_rejects_existing_output_before_running_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _write_manifest(tmp_path)
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")
    called = False

    def fake_run_arm(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("arm must not run")

    monkeypatch.setattr(experiment, "run_arm", fake_run_arm)

    exit_code = experiment.main(
        [
            "run",
            "--arm",
            "jev",
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert called is False
