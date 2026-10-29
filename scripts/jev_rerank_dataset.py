from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.golden_corpus_snapshot import (
    DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
    GoldenCorpusSnapshot,
    GoldenCorpusSnapshotError,
    load_golden_corpus_snapshot,
)
from backend.research.rag.evaluation.paper_evidence_eval import (
    EvidenceQAPair,
    load_evidence_golden_set,
)
from backend.research.rag.retrieval.paper_lightweight_reranker import (
    LightweightLexicalReranker,
)


DATASET_SCHEMA = "agora.jev-rerank-dataset/v1"
DEFAULT_GOLDEN_SET_PATH = Path("data/eval/golden_set.json")
DEFAULT_SAMPLE_SIZE = 24
DEFAULT_CANDIDATE_COUNT = 12
DEFAULT_SEED = "jev-rerank-pilot-v1"


class DatasetPreparationError(ValueError):
    """Raised when a reproducible experiment dataset cannot be prepared."""


def prepare_dataset(
    *,
    golden_set_path: str | Path = DEFAULT_GOLDEN_SET_PATH,
    snapshot_path: str | Path = DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    candidate_count: int = DEFAULT_CANDIDATE_COUNT,
    seed: str = DEFAULT_SEED,
) -> dict[str, Any]:
    """Build a fixed-input Jev reranking dataset from the reviewed snapshot."""
    golden_path = Path(golden_set_path)
    corpus_path = Path(snapshot_path)
    normalized_seed = _required_text(seed, "seed")
    _positive_int(sample_size, "sample_size")
    _positive_int(candidate_count, "candidate_count")

    golden_hash = _sha256_file(golden_path, "golden set")
    snapshot_hash = _sha256_file(corpus_path, "golden corpus snapshot")
    try:
        pairs = load_evidence_golden_set(golden_path)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as exc:
        raise DatasetPreparationError(
            f"cannot load golden set {golden_path}: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        snapshot = load_golden_corpus_snapshot(
            corpus_path,
            golden_set_path=golden_path,
        )
    except (
        GoldenCorpusSnapshotError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as exc:
        raise DatasetPreparationError(f"invalid golden corpus snapshot: {exc}") from exc

    return _build_dataset(
        pairs=pairs,
        snapshot=snapshot,
        sample_size=sample_size,
        candidate_count=candidate_count,
        seed=normalized_seed,
        golden_set_source={
            "path": _portable_source_path(golden_path),
            "sha256": golden_hash,
        },
        snapshot_source={
            "path": _portable_source_path(corpus_path),
            "sha256": snapshot_hash,
        },
    )


def manifest_fingerprint(payload: Mapping[str, Any]) -> str:
    """Return the content fingerprint used to bind experiment reports."""
    if not isinstance(payload, Mapping):
        raise DatasetPreparationError("manifest must be a JSON object")
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DatasetPreparationError("manifest must contain JSON values") from exc
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate a prepared reranking manifest for experiment execution."""
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetPreparationError(
            f"cannot read reranking manifest {manifest_path}: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise DatasetPreparationError("reranking manifest must be a JSON object")
    _validate_manifest(payload)
    return payload


def _validate_manifest(payload: Mapping[str, Any]) -> None:
    _exact_fields(payload, {"schema", "metadata", "cases"}, "reranking manifest")
    if payload["schema"] != DATASET_SCHEMA:
        raise DatasetPreparationError(
            f"unsupported reranking manifest schema: {payload['schema']}"
        )
    if not isinstance(payload["metadata"], Mapping):
        raise DatasetPreparationError("manifest metadata must be an object")
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise DatasetPreparationError("manifest cases must be a non-empty array")
    case_ids: set[str] = set()
    for case_index, case in enumerate(cases):
        label = f"manifest case {case_index}"
        if not isinstance(case, Mapping):
            raise DatasetPreparationError(f"{label} must be an object")
        _exact_fields(
            case,
            {"id", "paper_id", "query", "gold_ids", "candidates"},
            label,
        )
        case_id = _required_text(case["id"], f"{label} id")
        if case_id in case_ids:
            raise DatasetPreparationError(f"duplicate manifest case id: {case_id}")
        case_ids.add(case_id)
        _required_text(case["paper_id"], f"{label} paper_id")
        _required_text(case["query"], f"{label} query")
        _text_array(case["gold_ids"], f"{label} gold_ids")
        candidates = case["candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise DatasetPreparationError(f"{label} candidates must be a non-empty array")
        candidate_ids: set[str] = set()
        for candidate_index, candidate in enumerate(candidates):
            candidate_label = f"{label} candidate {candidate_index}"
            if not isinstance(candidate, Mapping):
                raise DatasetPreparationError(f"{candidate_label} must be an object")
            _exact_fields(
                candidate,
                {"id", "text", "paper_id", "source_ref"},
                candidate_label,
            )
            candidate_id = _required_text(candidate["id"], f"{candidate_label} id")
            if candidate_id in candidate_ids:
                raise DatasetPreparationError(
                    f"{label} has duplicate candidate id: {candidate_id}"
                )
            candidate_ids.add(candidate_id)
            _required_text(candidate["text"], f"{candidate_label} text")
            _required_text(candidate["paper_id"], f"{candidate_label} paper_id")
            _required_text(candidate["source_ref"], f"{candidate_label} source_ref")


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise DatasetPreparationError(f"{label} fields invalid ({'; '.join(details)})")


def _text_array(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise DatasetPreparationError(f"{label} must be a non-empty array")
    texts = [_required_text(item, f"{label} item") for item in value]
    if len(texts) != len(set(texts)):
        raise DatasetPreparationError(f"{label} must contain unique values")
    return texts


def _build_dataset(
    *,
    pairs: Sequence[EvidenceQAPair],
    snapshot: GoldenCorpusSnapshot,
    sample_size: int,
    candidate_count: int,
    seed: str,
    golden_set_source: Mapping[str, str],
    snapshot_source: Mapping[str, str],
) -> dict[str, Any]:
    if not pairs:
        raise DatasetPreparationError("golden set must contain at least one question")
    answerable = [pair for pair in pairs if pair.expected_behavior == "answer"]
    abstain_count = sum(pair.expected_behavior == "abstain" for pair in pairs)
    unexpected = sorted(
        {
            str(pair.expected_behavior)
            for pair in pairs
            if pair.expected_behavior not in {"answer", "abstain"}
        }
    )
    if unexpected:
        raise DatasetPreparationError(
            "golden set contains unsupported expected_behavior values: "
            + ", ".join(unexpected)
        )
    if not answerable:
        raise DatasetPreparationError("golden set has no answerable questions")
    if sample_size > len(answerable):
        raise DatasetPreparationError(
            f"sample_size {sample_size} exceeds {len(answerable)} answerable questions"
        )

    chunks = _validated_chunks(snapshot.chunks)
    if candidate_count > len(chunks):
        raise DatasetPreparationError(
            f"candidate_count {candidate_count} exceeds {len(chunks)} snapshot chunks"
        )
    sampled = sorted(
        answerable,
        key=lambda pair: (
            _selection_key(seed, pair.paper_id, pair.question),
            pair.paper_id,
            pair.question,
        ),
    )[:sample_size]
    identities = [_case_id(pair) for pair in sampled]
    if len(identities) != len(set(identities)):
        raise DatasetPreparationError("sample contains duplicate stable case identities")

    reranker = LightweightLexicalReranker()
    cases: list[dict[str, Any]] = []
    covered_case_count = 0
    for pair, case_id in zip(sampled, identities, strict=True):
        ranked_chunks = _rank_chunks(
            query=pair.question,
            chunks=chunks,
            candidate_count=candidate_count,
            reranker=reranker,
        )
        gold_ids = sorted(set(pair.gold_chunk_ids))
        if not gold_ids:
            raise DatasetPreparationError(
                f"answerable case {case_id} has no gold chunk ids"
            )
        candidate_ids = {chunk.chunk_id for chunk in ranked_chunks}
        if candidate_ids.intersection(gold_ids):
            covered_case_count += 1
        cases.append(
            {
                "id": case_id,
                "paper_id": pair.paper_id,
                "query": pair.question,
                "gold_ids": gold_ids,
                "candidates": [_candidate_payload(chunk) for chunk in ranked_chunks],
            }
        )

    return {
        "schema": DATASET_SCHEMA,
        "metadata": {
            "seed": seed,
            "sample_size": sample_size,
            "candidate_count": candidate_count,
            "sources": {
                "golden_set": dict(golden_set_source),
                "snapshot": dict(snapshot_source),
            },
            "sampling_policy": {
                "population": "answerable_only",
                "selection": "ascending_sha256(seed\\0paper_id\\0query)",
                "case_id": "case_ + first_16_hex(sha256(paper_id\\0query))",
            },
            "candidate_policy": {
                "pool": "all_validated_snapshot_chunks",
                "selector": (
                    "backend.research.rag.retrieval.paper_lightweight_reranker."
                    "LightweightLexicalReranker"
                ),
                "ordering": "score_desc_then_chunk_id_asc",
                "gold_used_for_selection": False,
                "gold_injection": False,
            },
            "counts": {
                "golden_questions": len(pairs),
                "answerable_questions": len(answerable),
                "abstain_questions_excluded": abstain_count,
                "sampled_cases": len(cases),
                "snapshot_chunks": len(chunks),
                "snapshot_papers": len({chunk.paper_id for chunk in chunks}),
                "cases_with_candidate_gold": covered_case_count,
                "cases_without_candidate_gold": len(cases) - covered_case_count,
            },
            "limitations": [
                "Pilot uses a 35-chunk reviewed snapshot because the full local corpus is absent.",
                (
                    "Candidates are selected across papers and do not model "
                    "paper-scoped production recall."
                ),
                (
                    "Gold labels are incomplete and candidate gold coverage is only "
                    "a reranking upper bound."
                ),
                (
                    "BGE may truncate inputs at its native 512-token limit; "
                    "candidate text is preserved here."
                ),
            ],
        },
        "cases": cases,
    }


def _validated_chunks(chunks: Sequence[PaperChunk]) -> list[PaperChunk]:
    if not chunks:
        raise DatasetPreparationError("golden corpus snapshot contains no chunks")
    ordered = sorted(chunks, key=lambda chunk: chunk.chunk_id)
    chunk_ids = [chunk.chunk_id for chunk in ordered]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise DatasetPreparationError("golden corpus snapshot contains duplicate chunk ids")
    for chunk in ordered:
        _required_text(chunk.chunk_id, "chunk id")
        _required_text(chunk.paper_id, f"paper_id for {chunk.chunk_id}")
        _required_text(chunk.content, f"content for {chunk.chunk_id}")
        _required_text(
            chunk.metadata.get("source_ref"),
            f"source_ref for {chunk.chunk_id}",
        )
    return ordered


def _rank_chunks(
    *,
    query: str,
    chunks: Sequence[PaperChunk],
    candidate_count: int,
    reranker: LightweightLexicalReranker,
) -> list[PaperChunk]:
    scores = reranker.score(query, [chunk.content for chunk in chunks])
    if len(scores) != len(chunks):
        raise DatasetPreparationError("lexical reranker returned the wrong score count")
    ranked = sorted(
        zip(chunks, scores, strict=True),
        key=lambda item: (-item[1], item[0].chunk_id),
    )
    return [chunk for chunk, _score in ranked[:candidate_count]]


def _candidate_payload(chunk: PaperChunk) -> dict[str, str]:
    return {
        "id": chunk.chunk_id,
        "text": chunk.content,
        "paper_id": chunk.paper_id,
        "source_ref": str(chunk.metadata["source_ref"]),
    }


def _selection_key(seed: str, paper_id: str, query: str) -> str:
    return hashlib.sha256(f"{seed}\0{paper_id}\0{query}".encode("utf-8")).hexdigest()


def _case_id(pair: EvidenceQAPair) -> str:
    digest = hashlib.sha256(
        f"{pair.paper_id}\0{pair.question}".encode("utf-8")
    ).hexdigest()
    return f"case_{digest[:16]}"


def _sha256_file(path: Path, label: str) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DatasetPreparationError(
            f"cannot read {label} {path}: {type(exc).__name__}"
        ) from exc
    if not payload:
        raise DatasetPreparationError(f"{label} {path} must not be empty")
    return hashlib.sha256(payload).hexdigest()


def _portable_source_path(path: Path) -> str:
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DatasetPreparationError(f"{label} must be text")
    text = value.strip()
    if not text:
        raise DatasetPreparationError(f"{label} must be non-empty")
    return text


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DatasetPreparationError(f"{label} must be a positive integer")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare the fixed-input Jev reranking pilot dataset."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--candidate-count", type=int, default=DEFAULT_CANDIDATE_COUNT)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--golden-set", type=Path, default=DEFAULT_GOLDEN_SET_PATH)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_GOLDEN_CORPUS_SNAPSHOT_PATH,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    output: Path = args.output
    if output.exists():
        parser.error(f"refusing to overwrite existing output: {output}")
    try:
        manifest = prepare_dataset(
            golden_set_path=args.golden_set,
            snapshot_path=args.snapshot,
            sample_size=args.sample_size,
            candidate_count=args.candidate_count,
            seed=args.seed,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        parser.error(f"refusing to overwrite existing output: {output}")
    except DatasetPreparationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATASET_SCHEMA",
    "DatasetPreparationError",
    "load_manifest",
    "main",
    "manifest_fingerprint",
    "prepare_dataset",
]
