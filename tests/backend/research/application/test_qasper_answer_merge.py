from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import replace

import pytest

from backend.research.application.qasper_answer_merge import (
    QasperAnswerMergeError,
    merge_answer_runs,
    prepare_failed_answer_recovery,
    require_same_measurement,
)
from backend.research.rag.evaluation.qasper_answer_judge import JudgeVerdict
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerCase, QasperAnswerSummary
from framework.rag.evaluation.answer_metrics import AnswerOutcome


class _Artifacts:
    def __init__(self, runs: Mapping[str, Mapping[str, bytes]]) -> None:
        self.objects = {(directory, name): value for directory, files in runs.items() for name, value in files.items()}
        self.reads: dict[tuple[str, str], int] = {}
        self.publishes: list[str] = []

    def read(self, directory: str, name: str) -> bytes:
        key = (directory, name)
        self.reads[key] = self.reads.get(key, 0) + 1
        return self.objects[key]

    def publish(self, directory: str, objects: Mapping[str, bytes]) -> None:
        if any(key[0] == directory for key in self.objects):
            raise AssertionError("attempted overwrite")
        self.publishes.append(directory)
        for name, value in objects.items():
            self.objects[(directory, name)] = value


def _identity(*, code: str, prompt: str = "answer-prompt-v1") -> dict:
    return {
        "dataset_sha256": "dataset-sha",
        "mapping_digest": "mapping-sha",
        "embedding_cache_sha256": "cache-sha",
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": 1024,
        "retrieval_policy": "default",
        "retrieval_policy_settings": {"name": "default", "overfetch": 3},
        "retrieval_profile_fingerprint": "profile-sha",
        "retrieval_profile": {"field_index": False},
        "retrieval_components": {"chunk": "CachedFixtureVectorStore"},
        "context_budget": {"max_items": 8, "max_total_tokens": 2048},
        "context_runtime": {"name": "default"},
        "answer_prompt_version": prompt,
        "answer_protocol_version": "paper-answer-v2",
        "answer_model_identity": {
            "provider": "test",
            "model": "answer-model",
            "max_tokens": 600,
            "temperature": 0.0,
            "endpoint_sha256": "endpoint-sha",
        },
        "answer_models": ["answer-model"],
        "judge_prompt_version": None,
        "code": {"git_revision": code, "sources": [], "changes": []},
    }


def _case(
    case_id: str, *, outcome: AnswerOutcome, answer: str = "", judge: JudgeVerdict | None = None,
) -> QasperAnswerCase:
    answered = outcome is AnswerOutcome.ANSWERED
    return QasperAnswerCase(
        case_id=case_id,
        paper_id="paper",
        question=f"Question {case_id}?",
        stratum="extractive",
        references=(f"reference-{case_id}",),
        reference_types=("extractive",),
        mapping_disposition="valid",
        outcome=outcome,
        answer=answer if answered else "",
        prediction=answer if answered else "",
        answer_f1=1.0 if answered else 0.0,
        matched_type="extractive",
        cited_chunk_ids=("chunk",) if answered else (),
        context_chunk_ids=("chunk",),
        strict_citation_coverage=1.0 if answered else None,
        equivalent_citation_coverage=1.0 if answered else None,
        strict_context_coverage=1.0,
        equivalent_context_coverage=1.0,
        judge=judge,
        decision_reason="answered" if answered else "failed",
        abstention_reason="",
        end_to_end_ms=10.0,
        generation_ms=5.0 if answered else None,
        prompt_chars=100 if answered else None,
        model="answer-model",
        model_usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        generation_error="gateway" if outcome.is_system_failure else "",
    )


def _files(
    cases: list[QasperAnswerCase], *, identity: dict, sample: dict, selection: dict,
    recovery_source: dict | None = None, extras: dict[str, dict] | None = None,
) -> dict[str, bytes]:
    rows = []
    for case in cases:
        row = case.to_dict()
        row.update((extras or {}).get(case.case_id, {}))
        rows.append(row)
    report = {
        "protocol": "qasper_end_to_end_answer_v1",
        "identity": identity,
        "sample": copy.deepcopy(sample),
        "evaluated_cases": len(cases),
        "case_selection": selection,
        "metrics": QasperAnswerSummary(tuple(cases)).to_dict(),
    }
    if recovery_source is not None:
        report["recovery_source"] = recovery_source
    return {
        "sample.json": json.dumps(sample, sort_keys=True).encode(),
        "answers.jsonl": b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows),
        "report.json": json.dumps(report, sort_keys=True).encode(),
    }


def _fixture() -> tuple[_Artifacts, list[QasperAnswerCase]]:
    judge_failed = JudgeVerdict(correct=None, error="judge_call_failed")
    base_cases = [
        _case("q1", outcome=AnswerOutcome.ANSWERED, answer="old-good"),
        _case("q2", outcome=AnswerOutcome.FAILED),
        _case("q3", outcome=AnswerOutcome.HALTED),
        _case("q4", outcome=AnswerOutcome.ANSWERED, answer="ungraded", judge=judge_failed),
    ]
    sample = {"case_ids": [case.case_id for case in base_cases], "seed": "seed", "per_type": 1}
    base = _files(
        base_cases, identity=_identity(code="base"), sample=sample,
        selection={"mode": "full_sample"}, extras={"q1": {"unknown_future_field": {"kept": True}}},
    )
    source = prepare_failed_answer_recovery(_Artifacts({"base": base}), "base")
    rerun_cases = [
        _case("q2", outcome=AnswerOutcome.ANSWERED, answer="recovered"),
        _case("q3", outcome=AnswerOutcome.FAILED),
    ]
    rerun = _files(
        rerun_cases, identity=_identity(code="rerun"), sample=sample,
        selection={"mode": "case_ids", "case_ids": ["q2", "q3"]}, recovery_source=source,
    )
    return _Artifacts({"base": base, "rerun": rerun}), base_cases


def _rows(store: _Artifacts, directory: str = "output") -> list[dict]:
    return [json.loads(line) for line in store.objects[(directory, "answers.jsonl")].splitlines()]


def test_prepare_selects_all_and_only_generation_failures_in_sample_order() -> None:
    artifacts, _ = _fixture()
    prepared = prepare_failed_answer_recovery(artifacts, "base")

    assert prepared["case_ids"] == ["q2", "q3"]
    assert prepared["seed"] == "seed"
    assert prepared["per_type"] == 1
    assert set(prepared["parent"]["sha256"]) == {"report", "answers", "sample"}
    assert prepared["parent"]["code"]["git_revision"] == "base"
    assert all(artifacts.reads[("base", name)] == 1 for name in ("report.json", "answers.jsonl", "sample.json"))


def test_merge_preserves_order_unknown_fields_and_failed_retry_with_cost_lineage() -> None:
    artifacts, _ = _fixture()
    originals = {key: value for key, value in artifacts.objects.items()}

    report = merge_answer_runs(
        artifacts, base_directory="base", rerun_directory="rerun", output_directory="output",
    )

    rows = _rows(artifacts)
    assert [row["case_id"] for row in rows] == ["q1", "q2", "q3", "q4"]
    assert rows[0]["unknown_future_field"] == {"kept": True}
    assert rows[1]["answer"] == "recovered"
    assert rows[2]["outcome"] == "failed"
    assert rows[3]["judge"]["error"] == "judge_call_failed"
    assert report["merge"]["replaced_case_ids"] == ["q2", "q3"]
    assert report["merge"]["still_generation_failures"] == ["q3"]
    assert [item["selected_source"] for item in report["merge"]["case_lineage"]] == ["base", "rerun", "rerun", "base"]
    assert report["cost_accounting"]["cumulative_attempt_tokens"]["total_tokens"] == 60
    assert report["identity"]["code"]["base"]["git_revision"] == "base"
    assert report["identity"]["code"]["rerun"]["git_revision"] == "rerun"
    assert artifacts.objects[("output", "sample.json")] == originals[("base", "sample.json")]
    assert {key: value for key, value in artifacts.objects.items() if key[0] in {"base", "rerun"}} == originals
    assert all(artifacts.reads[(directory, name)] == 1 for directory in ("base", "rerun") for name in ("report.json", "answers.jsonl", "sample.json"))


def test_require_same_measurement_rejects_missing_changed_and_one_sided_optional() -> None:
    identity = _identity(code="one")
    missing = copy.deepcopy(identity)
    missing.pop("answer_prompt_version")
    with pytest.raises(QasperAnswerMergeError, match="answer_prompt_version"):
        require_same_measurement(missing, missing)

    changed = copy.deepcopy(identity)
    changed["context_budget"]["max_items"] = 9
    with pytest.raises(QasperAnswerMergeError, match="context_budget"):
        require_same_measurement(identity, changed)

    one_sided = copy.deepcopy(identity)
    one_sided["judge_model_identity"] = {"provider": "x", "model": "judge"}
    with pytest.raises(QasperAnswerMergeError, match="one-sided missing"):
        require_same_measurement(identity, one_sided)


def test_measurement_ignores_observed_model_list_but_rejects_unknown_model_configuration() -> None:
    source = _identity(code="one")
    preflight = copy.deepcopy(source)
    preflight["answer_models"] = []
    require_same_measurement(source, preflight)

    unknown = copy.deepcopy(source)
    unknown["answer_model_identity"] = {"provider": "unknown", "model": "unknown"}
    with pytest.raises(QasperAnswerMergeError, match="requires known provider and model"):
        require_same_measurement(unknown, unknown)

    missing_temperature = copy.deepcopy(source)
    missing_temperature["answer_model_identity"].pop("temperature")
    with pytest.raises(QasperAnswerMergeError, match="explicit temperature"):
        require_same_measurement(missing_temperature, missing_temperature)

    explicit_default = copy.deepcopy(source)
    explicit_default["answer_model_identity"]["temperature"] = None
    require_same_measurement(explicit_default, explicit_default)


def test_prepare_rejects_observed_case_model_that_differs_from_configuration() -> None:
    artifacts, _ = _fixture()
    rows = _rows(artifacts, "base")
    rows[0]["model"] = "different-model"
    artifacts.objects[("base", "answers.jsonl")] = b"".join(
        json.dumps(row).encode() + b"\n" for row in rows
    )
    report = json.loads(artifacts.objects[("base", "report.json")])
    report["identity"]["answer_models"] = ["answer-model", "different-model"]
    artifacts.objects[("base", "report.json")] = json.dumps(report).encode()

    with pytest.raises(QasperAnswerMergeError, match="case.model differs"):
        prepare_failed_answer_recovery(artifacts, "base")


def test_merge_rejects_duplicate_case_ids_without_publishing() -> None:
    artifacts, _ = _fixture()
    first = artifacts.objects[("rerun", "answers.jsonl")].splitlines()[0]
    artifacts.objects[("rerun", "answers.jsonl")] += first + b"\n"
    report = json.loads(artifacts.objects[("rerun", "report.json")])
    report["evaluated_cases"] = 3
    artifacts.objects[("rerun", "report.json")] = json.dumps(report).encode()

    with pytest.raises(QasperAnswerMergeError, match="duplicate"):
        merge_answer_runs(artifacts, base_directory="base", rerun_directory="rerun", output_directory="output")
    assert not artifacts.publishes


def test_merge_rejects_normal_case_overwrite() -> None:
    artifacts, _ = _fixture()
    rerun = _rows(artifacts, "rerun")
    rerun.insert(0, _rows(artifacts, "base")[0])
    artifacts.objects[("rerun", "answers.jsonl")] = b"".join(json.dumps(row).encode() + b"\n" for row in rerun)
    report = json.loads(artifacts.objects[("rerun", "report.json")])
    report["evaluated_cases"] = 3
    report["case_selection"]["case_ids"] = ["q1", "q2", "q3"]
    artifacts.objects[("rerun", "report.json")] = json.dumps(report).encode()

    with pytest.raises(QasperAnswerMergeError):
        merge_answer_runs(artifacts, base_directory="base", rerun_directory="rerun", output_directory="output")
    assert not artifacts.publishes


def test_merge_rejects_tampered_parent_bound_by_recovery_source_hashes() -> None:
    artifacts, _ = _fixture()
    artifacts.objects[("base", "answers.jsonl")] += b"\n"

    with pytest.raises(QasperAnswerMergeError, match="recovery_source"):
        merge_answer_runs(artifacts, base_directory="base", rerun_directory="rerun", output_directory="output")
    assert not artifacts.publishes


def test_merge_rejects_changed_question_or_reference_identity() -> None:
    artifacts, _ = _fixture()
    rows = _rows(artifacts, "rerun")
    rows[0]["question"] = "A different question?"
    artifacts.objects[("rerun", "answers.jsonl")] = b"".join(json.dumps(row).encode() + b"\n" for row in rows)

    with pytest.raises(QasperAnswerMergeError, match="question/reference identity"):
        merge_answer_runs(artifacts, base_directory="base", rerun_directory="rerun", output_directory="output")


def test_merge_rejects_identity_mismatch_and_missing_artifact() -> None:
    artifacts, _ = _fixture()
    report = json.loads(artifacts.objects[("rerun", "report.json")])
    report["identity"]["answer_prompt_version"] = "changed"
    artifacts.objects[("rerun", "report.json")] = json.dumps(report).encode()
    with pytest.raises(QasperAnswerMergeError, match="answer_prompt_version"):
        merge_answer_runs(artifacts, base_directory="base", rerun_directory="rerun", output_directory="output")

    artifacts, _ = _fixture()
    del artifacts.objects[("rerun", "sample.json")]
    with pytest.raises(QasperAnswerMergeError, match="required artifact"):
        merge_answer_runs(artifacts, base_directory="base", rerun_directory="rerun", output_directory="output")


def test_prepare_rejects_duplicate_json_keys_and_case_order_mismatch() -> None:
    artifacts, _ = _fixture()
    artifacts.objects[("base", "report.json")] = b'{"protocol":"a","protocol":"b"}'
    with pytest.raises(QasperAnswerMergeError, match="duplicate JSON key"):
        prepare_failed_answer_recovery(artifacts, "base")

    artifacts, _ = _fixture()
    rows = _rows(artifacts, "base")
    rows[0], rows[1] = rows[1], rows[0]
    artifacts.objects[("base", "answers.jsonl")] = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    with pytest.raises(QasperAnswerMergeError, match="order/count"):
        prepare_failed_answer_recovery(artifacts, "base")


def test_merge_refuses_parent_directory_as_output() -> None:
    artifacts, _ = _fixture()
    with pytest.raises(QasperAnswerMergeError, match="output directory"):
        merge_answer_runs(artifacts, base_directory="base", rerun_directory="rerun", output_directory="base")


def test_merged_observed_models_are_recomputed_and_descendant_can_be_prepared() -> None:
    failed = replace(
        _case("q1", outcome=AnswerOutcome.FAILED),
        model="",
        model_usage=None,
    )
    sample = {"case_ids": ["q1"], "seed": "seed", "per_type": 1}
    base_identity = _identity(code="base")
    base_identity["answer_models"] = []
    base = _files(
        [failed], identity=base_identity, sample=sample, selection={"mode": "full_sample"},
    )
    prepared = prepare_failed_answer_recovery(_Artifacts({"base": base}), "base")
    recovered = _case("q1", outcome=AnswerOutcome.ANSWERED, answer="recovered")
    rerun = _files(
        [recovered], identity=_identity(code="rerun"), sample=sample,
        selection={"mode": "case_ids", "case_ids": ["q1"]}, recovery_source=prepared,
    )
    artifacts = _Artifacts({"base": base, "rerun": rerun})

    report = merge_answer_runs(
        artifacts, base_directory="base", rerun_directory="rerun", output_directory="output",
    )

    assert report["identity"]["answer_models"] == ["answer-model"]
    descendant = prepare_failed_answer_recovery(artifacts, "output")
    assert descendant["case_ids"] == []


def test_no_generation_refusal_is_valid_and_unchanged_beside_recovered_failure() -> None:
    refusal = replace(
        _case("q1", outcome=AnswerOutcome.INSUFFICIENT_EVIDENCE),
        model="",
        model_usage=None,
        generation_error="",
        decision_reason="insufficient_evidence",
    )
    failure = replace(
        _case("q2", outcome=AnswerOutcome.FAILED),
        model="",
        model_usage=None,
    )
    sample = {"case_ids": ["q1", "q2"], "seed": "seed", "per_type": 1}
    base_identity = _identity(code="base")
    base_identity["answer_models"] = []
    base = _files(
        [refusal, failure],
        identity=base_identity,
        sample=sample,
        selection={"mode": "full_sample"},
        extras={"q1": {"future_refusal_detail": "preserve"}},
    )
    prepared = prepare_failed_answer_recovery(_Artifacts({"base": base}), "base")
    recovered = _case("q2", outcome=AnswerOutcome.ANSWERED, answer="recovered")
    rerun = _files(
        [recovered],
        identity=_identity(code="rerun"),
        sample=sample,
        selection={"mode": "case_ids", "case_ids": ["q2"]},
        recovery_source=prepared,
    )
    artifacts = _Artifacts({"base": base, "rerun": rerun})

    merge_answer_runs(
        artifacts, base_directory="base", rerun_directory="rerun", output_directory="output",
    )

    base_refusal = json.loads(base["answers.jsonl"].splitlines()[0])
    merged_refusal = _rows(artifacts)[0]
    assert merged_refusal == base_refusal
