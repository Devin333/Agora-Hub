from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair
from scripts import jev_rerank_dataset as dataset


def _chunk(chunk_id: str, content: str, *, paper_id: str = "paper-a") -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id=paper_id,
        parse_source="latex",
        content=content,
        metadata={"source_ref": f"arxiv://{paper_id}/latex"},
    )


def _pair(
    question: str,
    gold_id: str,
    *,
    paper_id: str = "paper-a",
) -> EvidenceQAPair:
    return EvidenceQAPair(
        question=question,
        paper_id=paper_id,
        qa_type="paragraph",
        gold_chunk_ids=[gold_id],
    )


def _prepare_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    pairs: list[EvidenceQAPair],
    chunks: list[PaperChunk],
    sample_size: int,
    candidate_count: int,
) -> dict:
    golden_path = tmp_path / "golden.json"
    snapshot_path = tmp_path / "snapshot.json"
    golden_path.write_text('[{"fixture":"golden"}]', encoding="utf-8")
    snapshot_path.write_text('{"fixture":"snapshot"}', encoding="utf-8")
    monkeypatch.setattr(dataset, "load_evidence_golden_set", lambda _path: pairs)
    monkeypatch.setattr(
        dataset,
        "load_golden_corpus_snapshot",
        lambda _path, *, golden_set_path: SimpleNamespace(chunks=tuple(chunks)),
    )
    return dataset.prepare_dataset(
        golden_set_path=golden_path,
        snapshot_path=snapshot_path,
        sample_size=sample_size,
        candidate_count=candidate_count,
        seed="fixture-seed",
    )


def test_prepare_dataset_is_deterministic_across_input_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pairs = [
        _pair("alpha evidence?", "chunk-a"),
        _pair("beta evidence?", "chunk-b", paper_id="paper-b"),
        _pair("gamma evidence?", "chunk-c", paper_id="paper-c"),
    ]
    chunks = [
        _chunk("chunk-a", "alpha evidence"),
        _chunk("chunk-b", "beta evidence", paper_id="paper-b"),
        _chunk("chunk-c", "gamma evidence", paper_id="paper-c"),
    ]
    first = _prepare_fixture(
        monkeypatch,
        tmp_path,
        pairs=pairs,
        chunks=chunks,
        sample_size=2,
        candidate_count=2,
    )
    second = _prepare_fixture(
        monkeypatch,
        tmp_path,
        pairs=list(reversed(pairs)),
        chunks=list(reversed(chunks)),
        sample_size=2,
        candidate_count=2,
    )

    assert first == second
    assert dataset.manifest_fingerprint(first) == dataset.manifest_fingerprint(second)


def test_candidate_selection_does_not_depend_on_gold_and_does_not_inject_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chunks = [
        _chunk("chunk-best", "rare anchor matches rare anchor exactly"),
        _chunk("chunk-gold-a", "unrelated material one"),
        _chunk("chunk-gold-b", "unrelated material two"),
    ]
    first = _prepare_fixture(
        monkeypatch,
        tmp_path,
        pairs=[_pair("rare anchor", "chunk-gold-a")],
        chunks=chunks,
        sample_size=1,
        candidate_count=1,
    )
    second = _prepare_fixture(
        monkeypatch,
        tmp_path,
        pairs=[_pair("rare anchor", "chunk-gold-b")],
        chunks=chunks,
        sample_size=1,
        candidate_count=1,
    )

    assert first["cases"][0]["candidates"] == second["cases"][0]["candidates"]
    assert first["cases"][0]["candidates"][0]["id"] == "chunk-best"
    assert first["cases"][0]["gold_ids"] == ["chunk-gold-a"]
    assert first["metadata"]["counts"]["cases_without_candidate_gold"] == 1


def test_prepare_dataset_excludes_abstention_questions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    abstain = EvidenceQAPair.negative(
        question="unanswerable question?",
        paper_id="paper-a",
    )
    manifest = _prepare_fixture(
        monkeypatch,
        tmp_path,
        pairs=[_pair("answerable question?", "chunk-a"), abstain],
        chunks=[_chunk("chunk-a", "answerable question")],
        sample_size=1,
        candidate_count=1,
    )

    assert [case["query"] for case in manifest["cases"]] == ["answerable question?"]
    assert manifest["metadata"]["counts"]["abstain_questions_excluded"] == 1


def test_manifest_fingerprint_uses_canonical_json_and_has_no_top_level_field() -> None:
    payload = {"schema": dataset.DATASET_SCHEMA, "metadata": {"b": 2, "a": "é"}, "cases": []}
    reordered = {"cases": [], "metadata": {"a": "é", "b": 2}, "schema": dataset.DATASET_SCHEMA}
    expected = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    assert dataset.manifest_fingerprint(payload) == f"sha256:{expected}"
    assert dataset.manifest_fingerprint(payload) == dataset.manifest_fingerprint(reordered)
    assert "fingerprint" not in payload


def test_load_manifest_round_trips_prepared_dataset_and_rejects_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _prepare_fixture(
        monkeypatch,
        tmp_path,
        pairs=[_pair("answerable question?", "chunk-a")],
        chunks=[_chunk("chunk-a", "answerable question")],
        sample_size=1,
        candidate_count=1,
    )
    output = tmp_path / "manifest.json"
    output.write_text(json.dumps(manifest), encoding="utf-8")

    assert dataset.load_manifest(output) == manifest

    manifest["fingerprint"] = "sha256:" + "0" * 64
    output.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(dataset.DatasetPreparationError, match="unknown=fingerprint"):
        dataset.load_manifest(output)


def test_cli_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    output.write_text("keep-me", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        dataset.main(["--output", str(output)])

    assert raised.value.code == 2
    assert output.read_text(encoding="utf-8") == "keep-me"


def test_real_snapshot_prepare_dataset_runs_and_preserves_candidate_text() -> None:
    manifest = dataset.prepare_dataset()
    snapshot_payload = json.loads(
        Path("data/eval/golden_corpus_snapshot.json").read_text(encoding="utf-8")
    )
    source_text = {
        chunk["chunk_id"]: chunk["content"] for chunk in snapshot_payload["chunks"]
    }

    assert manifest["schema"] == dataset.DATASET_SCHEMA
    assert len(manifest["cases"]) == 24
    assert all(len(case["candidates"]) == 12 for case in manifest["cases"])
    assert all(
        candidate["text"] == source_text[candidate["id"]]
        for case in manifest["cases"]
        for candidate in case["candidates"]
    )
    assert manifest["metadata"]["counts"] == {
        "golden_questions": 79,
        "answerable_questions": 67,
        "abstain_questions_excluded": 12,
        "sampled_cases": 24,
        "snapshot_chunks": 35,
        "snapshot_papers": 20,
        "cases_with_candidate_gold": manifest["metadata"]["counts"][
            "cases_with_candidate_gold"
        ],
        "cases_without_candidate_gold": manifest["metadata"]["counts"][
            "cases_without_candidate_gold"
        ],
    }
    assert (
        manifest["metadata"]["counts"]["cases_with_candidate_gold"]
        + manifest["metadata"]["counts"]["cases_without_candidate_gold"]
        == 24
    )
    assert all(
        not Path(source["path"]).is_absolute()
        for source in manifest["metadata"]["sources"].values()
    )
