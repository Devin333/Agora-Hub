from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from framework.rag.retrieval import weighted_component_score

from backend.research.document.models import PaperChunk
from backend.research.ports.chunk_store import ChunkStorePort
from backend.research.rag.adapters.paper_field_text import extract_field_texts
from backend.research.rag.formula_normalizer import FormulaRetrievalMetadata, normalize_formula_metadata
from backend.research.rag.retrieval.bm25_index import (
    PaperBM25Index,
    default_bm25_index_path,
    load_bm25_index,
)
from backend.research.rag.retrieval.channels.base import RankedHit, RankedList
from backend.research.rag.retrieval.trace import RetrievalDegradation, RetrievalTrace

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_SPARSE_STOP_WORDS = {
    "about",
    "caption",
    "define",
    "defined",
    "does",
    "evidence",
    "explain",
    "explained",
    "explanation",
    "equation",
    "fig",
    "figure",
    "for",
    "formula",
    "from",
    "how",
    "latex",
    "mathematical",
    "mean",
    "means",
    "paper",
    "relation",
    "reported",
    "reports",
    "result",
    "results",
    "show",
    "shows",
    "surrounding",
    "symbol",
    "symbols",
    "table",
    "the",
    "this",
    "what",
    "which",
    "with",
}


@dataclass(frozen=True)
class FormulaSparseScores:
    symbol_score: float = 0.0
    operator_score: float = 0.0
    label_score: float = 0.0
    structure_score: float = 0.0
    context_score: float = 0.0
    sparse_score: float = 0.0
    strategy: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "formula_symbol_score": round_score(self.symbol_score),
            "formula_operator_score": round_score(self.operator_score),
            "formula_label_score": round_score(self.label_score),
            "formula_structure_score": round_score(self.structure_score),
            "formula_context_score": round_score(self.context_score),
            "formula_sparse_score": round_score(self.sparse_score),
            "formula_sparse_strategy": self.strategy,
        }


class SparseLexicalChannel:
    name = "sparse_lexical"

    def __init__(self, chunk_store: ChunkStorePort) -> None:
        self._store = chunk_store
        self._bm25_cache: dict[str, tuple[tuple[int, int], PaperBM25Index]] = {}
        self._bm25_cache_lock = RLock()

    def recall(
        self,
        request: Any,
        plan: Any,
    ) -> RankedList:
        chunks = self.recall_chunks(
            paper_id=request.paper_id,
            query_text=request.question,
            filters=getattr(plan, "filters", {}) or {},
            limit=getattr(plan, "limit", 10),
            formula_sparse_enabled=bool(getattr(plan, "formula_sparse_enabled", False)),
            trace=getattr(plan, "trace", None),
        )
        return [
            RankedHit(
                chunk_id=chunk.chunk_id,
                score=score,
                channel=self.name,
                metadata=dict(chunk.metadata),
            )
            for chunk, score in chunks
        ]

    def recall_chunks(
        self,
        *,
        paper_id: str,
        query_text: str,
        filters: dict[str, Any],
        limit: int,
        formula_sparse_enabled: bool = False,
        trace: RetrievalTrace | None = None,
    ) -> list[tuple[PaperChunk, float]]:
        index, index_source, chunks = self._load_bm25_index(paper_id, trace)
        if not chunks:
            if trace is not None:
                _append_degradation_once(
                    trace,
                    code="sparse_inventory_empty",
                    stage="sparse_lexical",
                    paper_id=paper_id,
                    reason="ChunkStorePort.list_chunks returned no chunks for sparse lexical recall.",
                )
            return []
        scored_by_id: dict[str, tuple[PaperChunk, float]] = {}
        bm25_query = " ".join(sparse_query_tokens(query_text)) or query_text
        for hit in index.search(bm25_query, limit=max(limit * 3, limit)):
            chunk = hit.chunk
            if chunk.paper_id != paper_id or not chunk_matches_filters(chunk, filters):
                continue
            lexical_score = sparse_lexical_score(query_text, chunk)
            formula_scores = formula_sparse_scores(query_text, chunk) if formula_sparse_enabled else FormulaSparseScores()
            score = max(hit.score, lexical_score, formula_scores.sparse_score)
            if score <= 0.0:
                continue
            metadata = dict(chunk.metadata)
            metadata.update({
                "sparse_lexical_hit": True,
                "sparse_lexical_score": round_score(lexical_score),
                "sparse_bm25_score": round_score(hit.score),
                "sparse_lexical_strategy": "bm25_index",
                "sparse_candidate_source": index_source,
            })
            if formula_sparse_enabled:
                metadata.update(formula_scores.as_metadata())
                metadata["formula_sparse_hit"] = formula_scores.sparse_score > 0.0
            scored_by_id[chunk.chunk_id] = (chunk.model_copy(update={"metadata": metadata}), score)
        if formula_sparse_enabled:
            for chunk in chunks:
                if chunk.chunk_id in scored_by_id:
                    continue
                if chunk.paper_id != paper_id or not chunk_matches_filters(chunk, filters):
                    continue
                formula_scores = formula_sparse_scores(query_text, chunk)
                if formula_scores.sparse_score <= 0.0:
                    continue
                lexical_score = sparse_lexical_score(query_text, chunk)
                metadata = dict(chunk.metadata)
                metadata.update({
                    "sparse_lexical_hit": True,
                    "sparse_lexical_score": round_score(lexical_score),
                    "sparse_bm25_score": 0.0,
                    "sparse_lexical_strategy": "bm25_index_with_formula_fallback",
                    "sparse_candidate_source": index_source,
                    **formula_scores.as_metadata(),
                    "formula_sparse_hit": True,
                })
                scored_by_id[chunk.chunk_id] = (
                    chunk.model_copy(update={"metadata": metadata}),
                    formula_scores.sparse_score,
                )
        scored = list(scored_by_id.values())
        scored.sort(key=lambda item: (-item[1], item[0].section_index, item[0].chunk_id))
        return scored[:limit]

    def _load_bm25_index(
        self,
        paper_id: str,
        trace: RetrievalTrace | None,
    ) -> tuple[PaperBM25Index, str, list[PaperChunk]]:
        index_path = default_bm25_index_path(paper_id)
        signature = _file_signature(index_path)
        if signature is not None:
            with self._bm25_cache_lock:
                cached = self._bm25_cache.get(paper_id)
                if cached is not None and cached[0] == signature:
                    index = cached[1]
                    return index, "bm25_index", index.chunks
        try:
            index = load_bm25_index(paper_id, path=index_path)
            if signature is not None:
                with self._bm25_cache_lock:
                    self._bm25_cache[paper_id] = (signature, index)
            return index, "bm25_index", index.chunks
        except FileNotFoundError:
            if trace is not None:
                _append_degradation_once(
                    trace,
                    code="sparse_bm25_index_missing",
                    stage="sparse_lexical",
                    paper_id=paper_id,
                    reason="Persisted BM25 index was not found; rebuilding sparse lexical inventory from ChunkStorePort.list_chunks.",
                )
        except Exception as exc:
            if trace is not None:
                _append_degradation_once(
                    trace,
                    code="sparse_bm25_index_unreadable",
                    stage="sparse_lexical",
                    paper_id=paper_id,
                    reason=f"Persisted BM25 index could not be loaded: {exc}",
                )
        chunks = self._store.list_chunks(paper_id)
        return PaperBM25Index.build(paper_id, chunks), "bm25_list_chunks_fallback", chunks

    def invalidate_bm25_cache(self, paper_id: str | None = None) -> None:
        """Invalidate persisted-index cache after a paper reindex operation."""
        with self._bm25_cache_lock:
            if paper_id is None:
                self._bm25_cache.clear()
            else:
                self._bm25_cache.pop(str(paper_id), None)


def chunk_matches_filters(chunk: PaperChunk, filters: dict[str, Any]) -> bool:
    for key, expected in (filters or {}).items():
        actual = getattr(chunk, key, None)
        if actual is None:
            actual = chunk.metadata.get(key)
        if actual != expected:
            return False
    return True


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def sparse_lexical_score(query_text: str, chunk: PaperChunk) -> float:
    query_tokens = sparse_query_tokens(query_text)
    if not query_tokens:
        return 0.0
    field_texts = extract_field_texts(chunk)
    field_values = field_texts.non_empty()
    if not field_values:
        field_values = {"body": chunk.content}
    query_set = set(query_tokens)
    best = 0.0
    for field_name, text in field_values.items():
        field_tokens = set(sparse_query_tokens(text))
        if not field_tokens:
            continue
        overlap = len(query_set & field_tokens) / len(query_set)
        if field_name in {"caption", "equation", "table_rows", "table_columns", "visual_description", "referenced_text"}:
            overlap *= 1.1
        best = max(best, overlap)
    query_phrase = " ".join(query_tokens)
    content_phrase = " ".join(sparse_query_tokens("\n".join(field_values.values())))
    if query_phrase and query_phrase in content_phrase:
        best = max(best, 0.95)
    return clamp_score(best)


def formula_sparse_scores(query_text: str, chunk: PaperChunk) -> FormulaSparseScores:
    if not is_formula_chunk(chunk) and not _chunk_has_formula_context(chunk):
        return FormulaSparseScores()
    query_formula = normalize_formula_metadata(str(query_text or ""), content=str(query_text or ""))
    chunk_formula = _formula_metadata_for_chunk(chunk)

    query_symbols = _casefold_set([*query_formula.symbols])
    query_operators = _casefold_set([*query_formula.operators])
    query_labels = {
        _normalize_element_label(value)
        for value in [*query_formula.reference_labels, *_element_query_labels(query_text, "formula_query")]
        if _normalize_element_label(value)
    }
    query_structure = _casefold_set(query_formula.structure_tokens)
    query_context = _casefold_set([
        *query_formula.symbols,
        *query_formula.operators,
        *query_formula.structure_tokens,
        *query_formula.context_terms,
        *sparse_query_tokens(query_formula.normalized_latex),
        *sparse_query_tokens(query_text),
    ])

    chunk_symbols = _casefold_set([
        *chunk_formula.symbols,
        *_metadata_text_values(chunk.metadata.get("formula_symbols")),
    ])
    chunk_operators = _casefold_set([
        *chunk_formula.operators,
        *_metadata_text_values(chunk.metadata.get("formula_operators")),
    ])
    chunk_labels = {
        _normalize_element_label(value)
        for value in [
            *chunk_formula.reference_labels,
            *_metadata_text_values(chunk.metadata.get("formula_reference_labels")),
            *_metadata_text_values(chunk.metadata.get("reference_labels")),
            str(chunk.metadata.get("equation_id") or ""),
            str(chunk.metadata.get("equation_label") or ""),
            str(chunk.metadata.get("equation_number") or ""),
            str(chunk.metadata.get("element_label") or ""),
        ]
        if _normalize_element_label(value)
    }
    chunk_structure = _casefold_set([
        *chunk_formula.structure_tokens,
        *_metadata_text_values(chunk.metadata.get("formula_structure_tokens")),
    ])
    chunk_context = _casefold_set([
        *chunk_formula.symbols,
        *chunk_formula.operators,
        *chunk_formula.structure_tokens,
        *sparse_query_tokens(chunk_formula.normalized_latex),
        *chunk_formula.context_terms,
        *_metadata_text_values(chunk.metadata.get("formula_context_terms")),
        *sparse_query_tokens(chunk.formula_description),
        *sparse_query_tokens(_metadata_join(chunk.metadata.get("formula_referenced_text"))),
        *sparse_query_tokens(chunk.content),
    ])

    symbol_score = _overlap_score(query_symbols, chunk_symbols)
    operator_score = max(
        _overlap_score(query_operators, chunk_operators),
        _overlap_score(query_context, chunk_operators),
    )
    label_score = _overlap_score(query_labels, chunk_labels)
    structure_score = _overlap_score(query_structure, chunk_structure)
    context_score = _overlap_score(query_context, chunk_context)
    weighted = weighted_component_score(
        {
            "symbol": symbol_score,
            "operator": operator_score,
            "label": label_score,
            "structure": structure_score,
            "context": context_score,
        },
        {
            "symbol": 0.25,
            "operator": 0.20,
            "label": 0.30,
            "structure": 0.10,
            "context": 0.15,
        },
    )
    sparse_score = max(weighted, label_score, (symbol_score + operator_score) / 2.0)
    if sparse_score <= 0.0:
        return FormulaSparseScores()
    return FormulaSparseScores(
        symbol_score=round_score(symbol_score),
        operator_score=round_score(operator_score),
        label_score=round_score(label_score),
        structure_score=round_score(structure_score),
        context_score=round_score(context_score),
        sparse_score=round_score(clamp_score(sparse_score)),
        strategy="deterministic_formula_overlap",
    )


def sparse_query_tokens(text: str) -> list[str]:
    tokens = []
    for match in _TOKEN_RE.finditer(str(text or "")):
        token = match.group(0).casefold().strip()
        if len(token) <= 1 or token in _SPARSE_STOP_WORDS:
            continue
        tokens.append(token)
    return tokens


def is_formula_chunk(chunk: PaperChunk) -> bool:
    return chunk.chunk_type == "formula" or chunk.has_formula or bool(chunk.formula_latex)


def clamp_score(value: float | None) -> float:
    if value is None:
        return 0.0
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def round_score(value: float) -> float:
    return round(float(value), 6)


def _formula_metadata_for_chunk(chunk: PaperChunk) -> FormulaRetrievalMetadata:
    formula_latex = chunk.formula_latex or (chunk.content if is_formula_chunk(chunk) else "")
    return normalize_formula_metadata(
        formula_latex,
        formula_description=chunk.formula_description,
        content=chunk.content,
        metadata=chunk.metadata,
    )


def _chunk_has_formula_context(chunk: PaperChunk) -> bool:
    metadata = chunk.metadata
    return bool(
        metadata.get("formula_referenced_text")
        or metadata.get("formula_symbols")
        or metadata.get("formula_operators")
        or metadata.get("formula_reference_labels")
        or metadata.get("formula_context_terms")
        or metadata.get("formula_chunk_id")
        or metadata.get("linked_formula_chunk_id")
    )


def _overlap_score(query_terms: set[str], candidate_terms: set[str]) -> float:
    if not query_terms or not candidate_terms:
        return 0.0
    return clamp_score(len(query_terms & candidate_terms) / len(query_terms))


def _casefold_set(values: Iterable[Any]) -> set[str]:
    return {
        str(value).casefold().strip()
        for value in values
        if str(value or "").strip()
    }


def _metadata_text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_metadata_text_values(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_metadata_text_values(item))
        return out
    return [str(value).strip()] if str(value).strip() else []


def _metadata_join(value: Any) -> str:
    return " ".join(_metadata_text_values(value))


def _element_query_labels(query_text: str, intent: str) -> set[str]:
    prefixes_by_intent: dict[str, tuple[str, ...]] = {
        "formula_query": ("equation", "formula", "eq"),
        "table_query": ("table", "tab"),
        "figure_query": ("figure", "fig"),
        "numerical_result": ("table", "tab", "figure", "fig"),
    }
    prefixes = prefixes_by_intent.get(intent, ())
    if not prefixes:
        return set()
    normalized = query_text.casefold()
    labels: set[str] = set()
    for prefix in prefixes:
        pattern = rf"\b{re.escape(prefix)}(?:\.|\s+)([a-z0-9][a-z0-9._-]*)"
        for match in re.finditer(pattern, normalized):
            labels.add(_normalize_element_label(match.group(1)))
    return {label for label in labels if label}


def _normalize_element_label(value: str) -> str:
    text = str(value or "").casefold().strip()
    text = re.sub(r"^(?:equation|formula|eq|table|tab|figure|fig)\.?\s*", "", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _append_degradation_once(
    trace: RetrievalTrace,
    *,
    code: str,
    stage: str,
    paper_id: str,
    reason: str,
) -> None:
    trace.append_degradation_once(RetrievalDegradation(
        code=code,
        stage=stage,
        paper_id=paper_id,
        reason=reason,
    ))


__all__ = [
    "FormulaSparseScores",
    "SparseLexicalChannel",
    "chunk_matches_filters",
    "clamp_score",
    "formula_sparse_scores",
    "is_formula_chunk",
    "round_score",
    "sparse_lexical_score",
    "sparse_query_tokens",
]
