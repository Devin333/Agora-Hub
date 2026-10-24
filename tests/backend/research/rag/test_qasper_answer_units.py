from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from backend.research.document.models import PaperChunk
from backend.research.ports.qasper_query_embedding import QueryVectorSnapshot
from backend.research.rag.evaluation.qasper_answer_judge import (
    JudgeRequest, JudgeVerdict, judge_answer, parse_verdict,
)
from backend.research.rag.evaluation.qasper_answer_metrics import (
    UNANSWERABLE,
    max_answer_f1,
    project_prediction,
    references_for_question,
)
from backend.research.rag.evaluation.qasper_answer_sampling import sample_questions
from backend.research.rag.evaluation.qasper_query_vectors import QueryVectorError, QueryVectorResolver
from backend.research.rag.evaluation.qasper_retrieval import (
    CachedFixtureChunkStore,
    OfflineEmbeddingCache,
    QasperOfflineCacheError,
)
from framework.rag.evaluation.answer_metrics import AnswerOutcome
from infrastructure.research.qasper_query_vector_store import FilesystemQueryVectorStore


class _Embedder:
    def __init__(self, *, model_id: str = "dashscope/text-embedding-v4", dimension: int = 2) -> None:
        self._model_id, self._dimension = model_id, dimension
        self.calls: list[list[str]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_queries(self, texts):
        self.calls.append(list(texts))
        return [[3.0, 4.0][: self._dimension] for _ in texts]


class _MemoryStore:
    def __init__(self, snapshot: QueryVectorSnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.saves = 0

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot
        self.saves += 1


def _cache(*texts: str) -> OfflineEmbeddingCache:
    # Always cache one chunk text so the matrix has the 2-d shape the embedder declares.
    entries = ("chunk body", *texts)
    return OfflineEmbeddingCache.from_vectors(
        {OfflineEmbeddingCache.key(t): [1.0, 0.0] for t in entries}, cache_id="base",
    )


def test_resolver_prefers_base_cache_then_embeds_once_and_normalizes() -> None:
    embedder, store = _Embedder(), _MemoryStore()
    resolver = QueryVectorResolver(_cache("cached"), embedder=embedder, store=store, model_id="dashscope/text-embedding-v4")

    assert resolver("cached").tolist() == [1.0, 0.0]
    first = resolver("rewritten query")
    second = resolver("rewritten query")

    assert np.allclose(first, [0.6, 0.8])
    assert second is first
    assert embedder.calls == [["rewritten query"]]
    assert resolver.stats.to_dict() == {"base_hits": 1, "supplementary_hits": 1, "live_calls": 1}
    resolver.flush()
    resolver.flush()
    assert store.saves == 1
    assert store.snapshot.base_cache_sha256 == "base"


def test_resolver_reuses_persisted_vectors_without_calling_the_provider() -> None:
    key = OfflineEmbeddingCache.key("rewritten query")
    store = _MemoryStore(QueryVectorSnapshot("base", "dashscope/text-embedding-v4", 2, {key: (0.6, 0.8)}))
    embedder = _Embedder()
    resolver = QueryVectorResolver(_cache(), embedder=embedder, store=store, model_id="dashscope/text-embedding-v4")

    assert np.allclose(resolver("rewritten query"), [0.6, 0.8])
    assert embedder.calls == []


@pytest.mark.parametrize(
    ("snapshot", "embedder", "message"),
    [
        (QueryVectorSnapshot("other-base", "dashscope/text-embedding-v4", 2), _Embedder(), "different base cache"),
        (QueryVectorSnapshot("base", "openai/other", 2), _Embedder(), "different embedding model"),
        (None, _Embedder(model_id="openai/other"), "does not match cache model"),
        (None, _Embedder(dimension=3), "dimension"),
    ],
)
def test_resolver_rejects_identity_mismatches(snapshot, embedder, message) -> None:
    with pytest.raises(QueryVectorError, match=message):
        QueryVectorResolver(_cache(), embedder=embedder, store=_MemoryStore(snapshot), model_id="dashscope/text-embedding-v4")


def test_fixture_store_stays_offline_without_a_resolver() -> None:
    chunk = PaperChunk(chunk_id="c1", paper_id="p1", parse_source="latex", chunk_type="paragraph", content="body")
    store = CachedFixtureChunkStore([chunk], _cache("body"))

    with pytest.raises(QasperOfflineCacheError):
        store.search_with_scores("p1", "unseen query")


def test_filesystem_store_round_trips_and_replaces_atomically(tmp_path: Path) -> None:
    store = FilesystemQueryVectorStore(tmp_path / "vectors.npz")
    assert store.load() is None
    store.save(QueryVectorSnapshot("base", "dashscope/text-embedding-v4", 2, {"k1": (0.6, 0.8)}))
    store.save(QueryVectorSnapshot("base", "dashscope/text-embedding-v4", 2, {"k1": (0.6, 0.8), "k2": (1.0, 0.0)}))

    loaded = store.load()

    assert loaded.base_cache_sha256 == "base"
    assert sorted(loaded.vectors) == ["k1", "k2"]
    assert np.allclose(loaded.vectors["k1"], (0.6, 0.8))
    assert [p.name for p in tmp_path.iterdir()] == ["vectors.npz"]


def _qa(question_id: str, *answers: dict) -> dict:
    return {"question_id": question_id, "question": f"question {question_id}",
            "answers": [{"answer": {"unanswerable": False, "extractive_spans": [], "free_form_answer": "",
                                    "yes_no": None, "evidence": [], **answer}} for answer in answers]}


def test_references_follow_the_official_scorer() -> None:
    qa = _qa("q", {"extractive_spans": ["BERT", "GPT"]}, {"unanswerable": True}, {"yes_no": False})

    refs = references_for_question(qa)

    assert [(r.answer, r.answer_type) for r in refs] == [("BERT, GPT", "extractive"), (UNANSWERABLE, "none"), ("No", "boolean")]


def test_judge_request_snapshot_is_canonical_and_tamper_evident() -> None:
    request = JudgeRequest("Q?", ("A", "B"), "A [1]", ((1, "passage"),))

    restored = JudgeRequest.from_dict(request.to_dict())

    assert restored == request
    assert restored.digest() == request.digest()
    tampered = request.to_dict()
    tampered["answer"] = "B [1]"
    with pytest.raises(ValueError, match="digest mismatch"):
        JudgeRequest.from_dict(tampered)


def test_judge_verdict_decoder_does_not_coerce_false_strings() -> None:
    with pytest.raises(ValueError, match="supports"):
        JudgeVerdict.from_dict({
            "correct": False, "citations": [{"index": 1, "supports": "false"}],
            "rationale": "", "error": "",
        })


def test_judge_failure_preserves_redacted_bounded_provider_facts() -> None:
    class ProviderError(RuntimeError):
        status_code = 503
        retryable = True
        response_body = "Bearer super-secret-token upstream unavailable"

        def to_dict(self, *, redact=True):
            return {
                "message": str(self), "provider": "gateway", "model": "judge-model",
                "error_type": "upstream_response", "error_category": "provider_failure",
                "status_code": self.status_code, "retryable": self.retryable, "attempts": 2,
                "diagnostics": [{
                    "provider_error_type": "invalid_request_error",
                    "provider_error_code": "gateway_timeout",
                    "provider_message": "request failed with sk-abcdefghijk",
                    "raw_excerpt": self.response_body,
                    "raw_excerpt_truncated": True,
                    "raw_excerpt_limit": 1000,
                }],
            }

    async def fail(_prompt: str) -> str:
        raise ProviderError("gateway failed for sk-abcdefghijk")

    verdict = asyncio.run(judge_answer(fail, JudgeRequest("Q?", ("A",), "A", ())))

    assert verdict.error == "judge_call_failed: ProviderError"
    assert verdict.error_detail["http_status"] == 503
    assert verdict.error_detail["retryable"] is True
    assert verdict.error_detail["error_type"] == "upstream_response"
    assert verdict.error_detail["error_category"] == "provider_failure"
    assert verdict.error_detail["provider"] == "gateway"
    assert verdict.error_detail["model"] == "judge-model"
    assert verdict.error_detail["attempts"] == 2
    assert verdict.error_detail["provider_error_type"] == "invalid_request_error"
    assert verdict.error_detail["provider_error_code"] == "gateway_timeout"
    assert "***REDACTED***" in verdict.error_detail["provider_message"]
    assert "***REDACTED***" in verdict.error_detail["message"]
    assert verdict.error_detail["message_truncated"] is False
    assert verdict.error_detail["message_limit"] == 2000
    assert "super-secret-token" not in verdict.error_detail["raw_excerpt"]
    assert verdict.error_detail["raw_excerpt_truncated"] is True
    assert verdict.error_detail["raw_excerpt_limit"] == 1000


def test_invalid_judge_output_redacts_assignments_and_private_keys_before_truncation() -> None:
    raw = (
        'not json api_key="plain-value"\n'
        "-----BEGIN PRIVATE KEY-----\nplain-private-material\n-----END PRIVATE KEY-----"
    )

    verdict = parse_verdict(raw, cited_indexes=())

    excerpt = verdict.error_detail["raw_excerpt"]
    assert "plain-value" not in excerpt
    assert "plain-private-material" not in excerpt
    assert "***REDACTED***" in excerpt
    assert verdict.error_detail["raw_excerpt_limit"] == 2000


@pytest.mark.parametrize(
    ("question", "outcome", "answer", "expected"),
    [
        ("What is used?", AnswerOutcome.ABSTAINED, "", UNANSWERABLE),
        ("What is used?", AnswerOutcome.INSUFFICIENT_EVIDENCE, "", UNANSWERABLE),
        ("What is used?", AnswerOutcome.HALTED, "", ""),
        ("Do they use attention?", AnswerOutcome.ANSWERED, "Yes, the model uses attention [1].", "Yes"),
        ("Do they use attention?", AnswerOutcome.ANSWERED, "No. It relies on recurrence [2].", "No"),
        ("Do they use attention?", AnswerOutcome.ANSWERED, "Nothing in the passage [1].", "Nothing in the passage."),
        ("What model is used?", AnswerOutcome.ANSWERED, "Yes the BERT model [1][2].", "Yes the BERT model."),
    ],
)
def test_prediction_projection(question, outcome, answer, expected) -> None:
    assert project_prediction(question, outcome, answer) == expected


def test_abstention_on_unanswerable_gold_scores_full_f1() -> None:
    refs = references_for_question(_qa("q", {"unanswerable": True}))

    assert max_answer_f1(UNANSWERABLE, refs).score == 1.0


def test_sampling_is_deterministic_stratified_and_reports_short_strata() -> None:
    papers = {"p1": {"qas": [
        _qa("e1", {"extractive_spans": ["x"]}), _qa("e2", {"extractive_spans": ["y"]}), _qa("e3", {"extractive_spans": ["z"]}),
        _qa("b1", {"yes_no": True}), _qa("n1", {"unanswerable": True}),
    ]}}

    first = sample_questions(papers, seed="s", per_type=2)
    again = sample_questions(papers, seed="s", per_type=2)
    other = sample_questions(papers, seed="t", per_type=2)

    assert first.manifest() == again.manifest()
    assert first.selected_by_type() == {"extractive": 2, "abstractive": 0, "boolean": 1, "none": 1}
    assert first.manifest()["available_by_type"]["extractive"] == 3
    # A different seed reorders the over-full stratum but never changes the per-type quota.
    assert other.selected_by_type() == first.selected_by_type()
    assert other.manifest()["seed"] == "t"


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ('{"correct": true, "citations": [{"index": 1, "supports": true}], "rationale": "ok"}', ""),
        ('```json\n{"correct": false, "citations": [{"index": 1, "supports": false}]}\n```', ""),
        ("I think it is correct.", "judge_output_not_json"),
        ('{"citations": []}', "judge_output_missing_correct"),
        ('{"correct": true, "citations": [{"index": 2, "supports": true}]}', "judge_citations_do_not_match_answer"),
        ('{"correct": true, "citations": [{"index": true, "supports": true}]}', "judge_output_bad_citations"),
    ],
)
def test_judge_parsing_is_strict(raw: str, error: str) -> None:
    verdict = parse_verdict(raw, cited_indexes=[1])

    assert verdict.error == error
    assert verdict.judge_error is bool(error)
    if not error:
        assert verdict.citation_support_rate() in (0.0, 1.0)


def test_judge_call_failure_is_a_judge_error() -> None:
    async def failing(prompt: str) -> str:
        raise TimeoutError("judge timed out")

    verdict = asyncio.run(judge_answer(failing, JudgeRequest("Q?", ["A"], "A [1]", [(1, "passage")])))

    assert verdict.error == "judge_call_failed: TimeoutError"
    assert verdict.correct is None


def test_judge_prompt_includes_references_answer_and_untrusted_passages() -> None:
    prompt = JudgeRequest("What is used?", ["BERT", "BERT", UNANSWERABLE], "BERT [1]", [(1, "We use BERT.")]).prompt()

    assert prompt.count("- BERT") == 1
    assert "We use BERT." in prompt
    assert "untrusted" in prompt
