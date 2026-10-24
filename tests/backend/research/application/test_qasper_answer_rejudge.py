from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager

import pytest

from backend.research.application.qasper_answer_rejudge import (
    QasperAnswerRejudgeConfig,
    QasperAnswerRejudgeError,
    QasperAnswerRejudgeService,
)
from backend.research.application.qasper_assets import QasperAssetService, QasperCacheIdentity
from backend.research.ports.qasper_evaluation import QasperRawCodeIdentity
from backend.research.rag.evaluation.qasper_answer_judge import (
    JUDGE_PROMPT_VERSION,
    JudgeRequest,
    JudgeVerdict,
)
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerCase
from backend.research.rag.evaluation.qasper_mapping import freeze_qasper_dataset
from backend.research.rag.retrieval.paper_answer_generator import render_answer_context
from framework.rag.evaluation.answer_metrics import AnswerOutcome

JUDGE_IDENTITY = {
    "provider": "test",
    "model": "judge-v2",
    "max_tokens": 400,
    "temperature": 0.0,
    "endpoint_sha256": "a" * 64,
}


class _Artifacts:
    def __init__(self, source: dict[str, bytes]) -> None:
        self.objects = {("source", name): content for name, content in source.items()}
        self.reads: dict[tuple[str, str], int] = {}

    def read(self, directory: str, name: str) -> bytes:
        key = (directory, name)
        self.reads[key] = self.reads.get(key, 0) + 1
        return self.objects[key]

    def publish(self, directory: str, objects: dict[str, bytes]) -> None:
        for name, content in objects.items():
            key = (directory, name)
            if key in self.objects and self.objects[key] != content:
                raise AssertionError("attempted overwrite")
            self.objects[key] = content


def _request(answer: str = "BERT [1]") -> JudgeRequest:
    return JudgeRequest(
        question="Which model?", references=("BERT",), answer=answer,
        cited_passages=((1, "The model is BERT."),),
    )


def _case(
    case_id: str,
    *,
    judge: JudgeVerdict | None,
    request: JudgeRequest | None,
    outcome: AnswerOutcome = AnswerOutcome.ANSWERED,
) -> QasperAnswerCase:
    answered = outcome is AnswerOutcome.ANSWERED
    return QasperAnswerCase(
        case_id=case_id,
        paper_id="p",
        question="Which model?",
        stratum="extractive",
        references=("BERT",),
        reference_types=("extractive",),
        mapping_disposition="valid",
        outcome=outcome,
        answer="BERT [1]" if answered else "",
        prediction="BERT" if answered else "",
        answer_f1=1.0 if answered else 0.0,
        matched_type="extractive",
        cited_chunk_ids=("c1",) if answered else (),
        context_chunk_ids=("c1",) if answered else (),
        strict_citation_coverage=1.0 if answered else None,
        equivalent_citation_coverage=1.0 if answered else None,
        strict_context_coverage=1.0 if answered else None,
        equivalent_context_coverage=1.0 if answered else None,
        judge=judge,
        decision_reason="kept",
        abstention_reason="",
        end_to_end_ms=19.5,
        generation_ms=11.0,
        prompt_chars=123,
        model="answer-model",
        model_usage={"total_tokens": 9},
        evidence_type_gap_accepted=True,
        generation_error="",
        generation_error_detail=None,
        judge_request=request,
    )


def _source(cases: list[QasperAnswerCase], *, prompt_version: str = JUDGE_PROMPT_VERSION) -> dict[str, bytes]:
    sample = {"case_ids": [case.case_id for case in cases], "seed": "s", "per_type": 1}
    report = {
        "protocol": "qasper_end_to_end_answer_v1",
        "identity": {
            "judge_prompt_version": prompt_version,
            "judge_model_identity": JUDGE_IDENTITY,
            "code": {"git_revision": "source-code"},
        },
        "sample": sample,
        "evaluated_cases": len(cases),
        "case_selection": {"mode": "full_sample"},
        "metrics": {"efficiency": {"sentinel": "source"}},
    }
    return {
        "sample.json": json.dumps(sample).encode(),
        "report.json": json.dumps(report).encode(),
        "answers.jsonl": "".join(json.dumps(case.to_dict()) + "\n" for case in cases).encode(),
    }


def _rows(artifacts: _Artifacts, directory: str = "output") -> list[dict]:
    return [
        json.loads(line)
        for line in artifacts.objects[(directory, "answers.jsonl")].decode().splitlines()
        if line
    ]


def test_rejudge_recovers_only_failed_judge_and_preserves_answer_record() -> None:
    failed = _case("p:q1", judge=JudgeVerdict(None, error="judge_call_failed"), request=_request())
    good = _case("p:q2", judge=JudgeVerdict(True), request=_request())
    halted = _case("p:q3", judge=None, request=None, outcome=AnswerOutcome.HALTED)
    source = _source([failed, good, halted])
    raw_rows = [json.loads(line) for line in source["answers.jsonl"].decode().splitlines()]
    raw_rows[0]["future_extension"] = {"kept": True}
    source["answers.jsonl"] = "".join(json.dumps(row) + "\n" for row in raw_rows).encode()
    source_copy = copy.deepcopy(source)
    artifacts = _Artifacts(source)
    calls: list[str] = []
    progress = []

    async def judge(prompt: str) -> str:
        calls.append(prompt)
        return '{"correct":true,"citations":[{"index":1,"supports":true}],"rationale":"ok"}'

    report = QasperAnswerRejudgeService(
        artifacts,
        judge_call=judge,
        judge_model_identity=JUDGE_IDENTITY,
        code_identity={"git_revision": "current-code"},
        progress=progress.append,
        config=QasperAnswerRejudgeConfig("source", "output"),
    ).run()

    assert len(calls) == 1
    assert [(item.phase, item.case_id, item.recovered) for item in progress] == [
        ("before", "p:q1", None), ("after", "p:q1", True),
    ]
    output = _rows(artifacts)
    before = failed.to_dict()
    after = output[0]
    for key in set(before) - {"judge", "judge_request", "rejudge_attempt", "end_to_end_correct"}:
        assert after[key] == before[key]
    assert output[1] == good.to_dict()
    assert output[2] == halted.to_dict()
    assert after["judge"]["correct"] is True
    assert after["future_extension"] == {"kept": True}
    assert after["rejudge_attempt"]["attempt"] == 1
    assert report["rejudge"]["selected"] == 1
    assert report["rejudge"]["recovered"] == 1
    assert report["rejudge"]["attempts"] == 1
    assert report["rejudge"]["judge_calls"]["count"] == 1
    assert report["rejudge"]["judge_calls"]["elapsed_ms"] > 0
    assert report["metrics"]["efficiency"] == {"sentinel": "source"}
    assert report["identity"]["code"] == {"git_revision": "source-code"}
    assert report["rejudge"]["code_identity"] == {"git_revision": "current-code"}
    assert artifacts.objects[("output", "sample.json")] == source["sample.json"]
    assert {name: artifacts.objects[("source", name)] for name in source} == source_copy
    assert all(artifacts.reads[("source", name)] == 1 for name in source)
    assert report["rejudge"]["parents"]["answers.jsonl"]["sha256"] == hashlib.sha256(
        source["answers.jsonl"]
    ).hexdigest()


def test_missing_judge_is_opt_in_and_never_selects_non_answered() -> None:
    missing = _case("p:q1", judge=None, request=_request())
    halted = _case("p:q2", judge=None, request=None, outcome=AnswerOutcome.HALTED)
    calls = 0

    async def judge(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return '{"correct":true,"citations":[{"index":1,"supports":true}]}'

    artifacts = _Artifacts(_source([missing, halted]))
    QasperAnswerRejudgeService(
        artifacts, judge_call=judge, config=QasperAnswerRejudgeConfig("source", "default"),
    ).run()
    assert calls == 0

    artifacts = _Artifacts(_source([missing, halted]))
    QasperAnswerRejudgeService(
        artifacts, judge_call=judge, judge_model_identity=JUDGE_IDENTITY,
        config=QasperAnswerRejudgeConfig("source", "included", include_missing_judge=True),
    ).run()
    assert calls == 1


@pytest.mark.parametrize(
    "corruption", [
        "duplicate_ids", "request_tamper", "prompt_identity", "judge_identity", "empty_identity",
        "partial_identity", "unknown_selection",
    ],
)
def test_rejudge_fails_closed_before_judge_call(corruption: str) -> None:
    case = _case("p:q1", judge=JudgeVerdict(None, error="failed"), request=_request())
    cases = [case, case] if corruption == "duplicate_ids" else [case]
    source = _source(cases, prompt_version="other" if corruption == "prompt_identity" else JUDGE_PROMPT_VERSION)
    if corruption == "request_tamper":
        row = json.loads(source["answers.jsonl"])
        row["judge_request"]["answer"] = "tampered"
        source["answers.jsonl"] = (json.dumps(row) + "\n").encode()
    if corruption == "empty_identity":
        report = json.loads(source["report.json"])
        report["identity"]["judge_model_identity"] = {}
        source["report.json"] = json.dumps(report).encode()
    if corruption == "partial_identity":
        report = json.loads(source["report.json"])
        report["identity"]["judge_model_identity"] = {"provider": "test", "model": "judge-v2"}
        source["report.json"] = json.dumps(report).encode()
    if corruption == "unknown_selection":
        report = json.loads(source["report.json"])
        report["case_selection"] = {"mode": "future_mode"}
        source["report.json"] = json.dumps(report).encode()
    calls = 0

    async def judge(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "{}"

    with pytest.raises(QasperAnswerRejudgeError):
        QasperAnswerRejudgeService(
            _Artifacts(source), judge_call=judge,
            judge_model_identity=(
                {**JUDGE_IDENTITY, "provider": "other"}
                if corruption == "judge_identity"
                else {}
                if corruption == "empty_identity"
                else {"provider": "test", "model": "judge-v2"}
                if corruption == "partial_identity"
                else JUDGE_IDENTITY
            ),
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()
    assert calls == 0


def test_case_decoder_rejects_string_boolean() -> None:
    case = _case("p:q1", judge=JudgeVerdict(None, error="failed"), request=_request())
    source = _source([case])
    row = json.loads(source["answers.jsonl"])
    row["evidence_type_gap_accepted"] = "false"
    source["answers.jsonl"] = (json.dumps(row) + "\n").encode()

    async def judge(_prompt: str) -> str:
        return "{}"

    with pytest.raises(QasperAnswerRejudgeError, match="must be a boolean"):
        QasperAnswerRejudgeService(
            _Artifacts(source), judge_call=judge,
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()


def test_all_selected_requests_are_preflighted_before_first_judge_call() -> None:
    first = _case("p:q1", judge=JudgeVerdict(None, error="failed"), request=_request())
    second = _case("p:q2", judge=JudgeVerdict(None, error="failed"), request=_request())
    source = _source([first, second])
    rows = [json.loads(line) for line in source["answers.jsonl"].decode().splitlines()]
    rows[1]["judge_request"] = JudgeRequest(
        "Which model?", ("BERT",), "different [1]", ((1, "The model is BERT."),),
    ).to_dict()
    source["answers.jsonl"] = "".join(json.dumps(row) + "\n" for row in rows).encode()
    calls = 0

    async def judge(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return '{"correct":true,"citations":[{"index":1,"supports":true}]}'

    with pytest.raises(QasperAnswerRejudgeError, match="answer mismatch"):
        QasperAnswerRejudgeService(
            _Artifacts(source), judge_call=judge,
            judge_model_identity=JUDGE_IDENTITY,
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()
    assert calls == 0


@pytest.mark.parametrize(("answer", "message"), [("", "empty answer"), ("BERT [2]", "invalid citation")])
def test_selected_answer_must_have_valid_frozen_citation_markers(answer: str, message: str) -> None:
    case = _case("p:q1", judge=JudgeVerdict(None, error="failed"), request=_request())
    raw = case.to_dict()
    raw["answer"] = answer
    raw["judge_request"] = JudgeRequest(
        "Which model?", ("BERT",), answer, ((1, "The model is BERT."),),
    ).to_dict()
    source = _source([QasperAnswerCase.from_dict(raw)])

    async def judge(_prompt: str) -> str:
        return "{}"

    with pytest.raises(QasperAnswerRejudgeError, match=message):
        QasperAnswerRejudgeService(
            _Artifacts(source), judge_call=judge,
            judge_model_identity=JUDGE_IDENTITY,
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()


def test_legacy_case_reconstructs_original_request_from_frozen_assets() -> None:
    assets, source, chunk = _legacy_source()
    prompts: list[str] = []

    async def judge(prompt: str) -> str:
        prompts.append(prompt)
        return '{"correct":true,"citations":[{"index":1,"supports":true}]}'

    artifacts = _Artifacts(source)
    report = QasperAnswerRejudgeService(
        artifacts, assets=assets, judge_call=judge, judge_model_identity="legacy-judge",
        legacy_judge_model_identity="legacy-judge",
        config=QasperAnswerRejudgeConfig("source", "output"),
    ).run()

    assert len(prompts) == 1
    assert render_answer_context(chunk) in prompts[0]
    assert _rows(artifacts)[0]["judge_request"]["digest"]
    manifest = report["rejudge"]["legacy_reconstruction"]
    assert manifest["current_assets"]["corpus_sha256"]
    assert manifest["historical_identity_unavailable"] == ["corpus_sha256", "index_sha256"]
    assert manifest["legacy_judge_identity"]["comparison_scope"] == "model_only"
    assert manifest["legacy_judge_identity"]["unverified_fields"] == [
        "provider", "max_tokens", "temperature", "endpoint_sha256",
    ]
    assert report["identity"]["judge_models"]["source"] == "legacy-judge"


def test_legacy_mapping_uses_stripped_query_but_judge_request_keeps_original_question() -> None:
    assets, source, _chunk = _legacy_source(question="Which model?  ")
    prompts: list[str] = []

    async def judge(prompt: str) -> str:
        prompts.append(prompt)
        return '{"correct":true,"citations":[{"index":1,"supports":true}]}'

    artifacts = _Artifacts(source)
    QasperAnswerRejudgeService(
        artifacts, assets=assets, judge_call=judge,
        judge_model_identity="legacy-judge", legacy_judge_model_identity="legacy-judge",
        config=QasperAnswerRejudgeConfig("source", "output"),
    ).run()

    row = _rows(artifacts)[0]
    assert row["question"] == "Which model?  "
    assert row["judge_request"]["question"] == "Which model?  "
    assert "Question:\nWhich model?  \n" in prompts[0]


def test_legacy_case_requires_explicit_model_declaration_and_matching_assets() -> None:
    assets, source, _chunk = _legacy_source()

    async def judge(_prompt: str) -> str:
        return "{}"

    with pytest.raises(QasperAnswerRejudgeError, match="explicit legacy declaration"):
        QasperAnswerRejudgeService(
            _Artifacts(source), assets=assets, judge_call=judge,
            judge_model_identity="legacy-judge",
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()


def test_legacy_case_with_full_source_identity_rejects_configuration_drift_before_call() -> None:
    assets, source, _chunk = _legacy_source()
    report = json.loads(source["report.json"])
    report["identity"]["judge_model_identity"] = JUDGE_IDENTITY
    source["report.json"] = json.dumps(report).encode()
    calls = 0

    async def judge(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return '{"correct":true,"citations":[{"index":1,"supports":true}]}'

    with pytest.raises(QasperAnswerRejudgeError, match="does not match the source run"):
        QasperAnswerRejudgeService(
            _Artifacts(source), assets=assets, judge_call=judge,
            judge_model_identity={**JUDGE_IDENTITY, "temperature": 0.5},
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()
    assert calls == 0

    output = QasperAnswerRejudgeService(
        _Artifacts(source), assets=assets, judge_call=judge,
        judge_model_identity=JUDGE_IDENTITY,
        config=QasperAnswerRejudgeConfig("source", "output"),
    ).run()
    assert calls == 1
    legacy_identity = output["rejudge"]["legacy_reconstruction"]["legacy_judge_identity"]
    assert legacy_identity["comparison_scope"] == "full_configuration"
    assert legacy_identity["unverified_fields"] == []


def test_legacy_case_verifies_optional_historical_corpus_and_index_identity() -> None:
    assets, source, _chunk = _legacy_source()
    frozen = freeze_qasper_dataset(
        assets.loaded.raw_dataset, assets.loaded.documents, assets.loaded.chunks,
    )
    report = json.loads(source["report.json"])
    report["identity"].update({
        "corpus_sha256": frozen.corpus_sha256,
        "index_sha256": frozen.index_sha256,
    })
    source["report.json"] = json.dumps(report).encode()

    async def judge(_prompt: str) -> str:
        return '{"correct":true,"citations":[{"index":1,"supports":true}]}'

    output = QasperAnswerRejudgeService(
        _Artifacts(source), assets=assets, judge_call=judge,
        judge_model_identity="legacy-judge", legacy_judge_model_identity="legacy-judge",
        config=QasperAnswerRejudgeConfig("source", "output"),
    ).run()

    manifest = output["rejudge"]["legacy_reconstruction"]
    assert {"corpus_sha256", "index_sha256"} <= set(manifest["historical_identity_verified"])
    assert manifest["historical_identity_unavailable"] == []

    report = json.loads(source["report.json"])
    report["identity"]["dataset_sha256"] = "wrong"
    source["report.json"] = json.dumps(report).encode()
    with pytest.raises(QasperAnswerRejudgeError, match="dataset_sha256"):
        QasperAnswerRejudgeService(
            _Artifacts(source), assets=assets, judge_call=judge,
            judge_model_identity="legacy-judge",
            legacy_judge_model_identity="legacy-judge",
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()


def test_legacy_case_rejects_missing_frozen_chunk_before_judge_call() -> None:
    assets, source, _chunk = _legacy_source()
    row = json.loads(source["answers.jsonl"])
    row["context_chunk_ids"] = ["missing-chunk"]
    row["cited_chunk_ids"] = ["missing-chunk"]
    source["answers.jsonl"] = (json.dumps(row) + "\n").encode()
    calls = 0

    async def judge(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "{}"

    with pytest.raises(QasperAnswerRejudgeError, match="missing frozen chunk"):
        QasperAnswerRejudgeService(
            _Artifacts(source), assets=assets, judge_call=judge,
            judge_model_identity="legacy-judge",
            legacy_judge_model_identity="legacy-judge",
            config=QasperAnswerRejudgeConfig("source", "output"),
        ).run()
    assert calls == 0


def _legacy_source(*, question: str = "Which model?"):
    raw = {
        "p": {
            "title": "Paper",
            "abstract": "BERT is the model.",
            "full_text": [{"section_name": "Method", "paragraphs": ["The model is BERT."]}],
            "figures_and_tables": [],
            "qas": [{
                "question_id": "q1",
                "question": question,
                "answers": [{"answer": {
                    "unanswerable": False, "extractive_spans": ["BERT"], "free_form_answer": "",
                    "yes_no": None, "evidence": ["The model is BERT."], "highlighted_evidence": [],
                }}],
            }],
        }
    }
    asset_service = QasperAssetService(_AssetReader(json.dumps(raw).encode()))
    loaded = asset_service.read_assets()
    assets = _LegacyAssets(loaded)
    frozen = freeze_qasper_dataset(loaded.raw_dataset, loaded.documents, loaded.chunks)
    chunk = next(item for item in loaded.chunks if "The model is BERT" in item.content)
    case = _case("p:q1", judge=JudgeVerdict(None, error="failed"), request=None)
    case = QasperAnswerCase.from_dict({
        **case.to_dict(), "question": question,
        "context_chunk_ids": [chunk.chunk_id], "cited_chunk_ids": [chunk.chunk_id],
        "judge_request": None,
    })
    source = _source([case])
    report = json.loads(source["report.json"])
    report["identity"].pop("judge_model_identity")
    report.pop("case_selection")
    report["identity"].update({
        "dataset_sha256": loaded.dataset_sha256,
        "mapping_digest": frozen.digest,
        "embedding_cache_sha256": "cache-sha",
        "embedding_model": "embed-model",
        "embedding_dimension": 4,
        "retrieval_profile_fingerprint": "retrieval-fingerprint",
        "context_runtime": {"name": "default"},
    })
    source["report.json"] = json.dumps(report).encode()
    return assets, source, chunk


class _AssetReader:
    def __init__(self, dataset: bytes) -> None:
        self.dataset = dataset

    def read_dataset(self) -> bytes:
        return self.dataset

    @contextmanager
    def open_cache(self):
        raise AssertionError("legacy reconstruction must not load the embedding cache")
        yield

    def read_code_identity(self, *, source_roots):
        return QasperRawCodeIdentity("0" * 40, (), ())


class _LegacyAssets:
    def __init__(self, loaded) -> None:
        self.loaded = loaded

    def read_assets(self):
        return self.loaded

    def cache_identity(self) -> QasperCacheIdentity:
        return QasperCacheIdentity("cache-sha", 4, "embed-model", "embed-version")
