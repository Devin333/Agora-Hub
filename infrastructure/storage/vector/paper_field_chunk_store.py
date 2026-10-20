from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qdrant_client import models as qmodels

from infrastructure.storage.vector.models import VectorDocument, VectorSearchQuery
from infrastructure.storage.vector.qdrant_store import QdrantVectorStore

PAPER_FIELD_CHUNKS_COLLECTION = "paper_field_chunks"
CORE_FIELD_NAMES: tuple[str, ...] = ("title", "abstract", "caption", "equation", "body")
EXPANDED_FIELD_NAMES: tuple[str, ...] = (
    "table_rows",
    "table_columns",
    "visual_description",
    "referenced_text",
)
FIELD_NAMES: tuple[str, ...] = (*CORE_FIELD_NAMES, *EXPANDED_FIELD_NAMES)

_PAYLOAD_INDEXES: dict[str, str] = {
    "paper_id": "keyword",
    "chunk_id": "keyword",
    "field_name": "keyword",
    "chunk_type": "keyword",
    "has_formula": "bool",
    "has_figure": "bool",
    "has_table": "bool",
    "section_index": "integer",
}


@dataclass(frozen=True)
class FieldEmbeddingHit:
    chunk_id: str
    field_name: str
    score: float
    field_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _PaperChunkFieldText:
    title: str = ""
    abstract: str = ""
    caption: str = ""
    equation: str = ""
    body: str = ""
    table_rows: str = ""
    table_columns: str = ""
    visual_description: str = ""
    referenced_text: str = ""
    sources: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, str]:
        return {name: self.text_for(name) for name in FIELD_NAMES}

    def non_empty(self) -> dict[str, str]:
        return {name: text for name, text in self.as_dict().items() if text.strip()}

    def text_for(self, field_name: str) -> str:
        if field_name not in FIELD_NAMES:
            return ""
        return str(getattr(self, field_name))

    def sources_for(self, field_name: str) -> tuple[str, ...]:
        return tuple(self.sources.get(field_name, ()))


class PaperFieldChunkStore:
    """Field-level vector index for paper chunks."""

    def __init__(self, vector_store: QdrantVectorStore | Any) -> None:
        self._store = vector_store

    def ensure_collection(self) -> None:
        self._store.ensure_collections([PAPER_FIELD_CHUNKS_COLLECTION])
        self._store.ensure_payload_indexes([PAPER_FIELD_CHUNKS_COLLECTION], _PAYLOAD_INDEXES)

    def index_chunks(self, chunks: list[Any]) -> None:
        docs: list[VectorDocument] = []
        for chunk in chunks:
            docs.extend(_chunk_to_field_docs(chunk))
        if docs:
            self._store.upsert_documents(docs)

    def delete_paper_chunks(self, paper_id: str) -> None:
        if hasattr(self._store, "delete_by_filter"):
            self._store.delete_by_filter(PAPER_FIELD_CHUNKS_COLLECTION, {"paper_id": paper_id})
            return

        self._store.client.delete(
            collection_name=PAPER_FIELD_CHUNKS_COLLECTION,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key="paper_id", match=qmodels.MatchValue(value=paper_id))]
                )
            ),
        )

    def search_field_vectors(
        self,
        paper_id: str,
        query_text: str,
        *,
        field_names: tuple[str, ...] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[FieldEmbeddingHit]:
        names = _normalized_field_names(field_names)
        combined_filters = {"paper_id": paper_id, **(filters or {})}
        if names:
            # One Qdrant query over all requested fields avoids embedding and
            # network work once per field. Overfetch by the number of fields so
            # the final global top-k has the same candidate envelope as the
            # previous per-field queries.
            combined_filters["field_name"] = list(names)
            search_limit = max(limit, limit * len(names))
        else:
            search_limit = limit

        results = self._search_one_field(
            query_text,
            filters=combined_filters,
            limit=search_limit,
        )

        results.sort(key=lambda hit: hit.score, reverse=True)
        return results[:limit]

    def _search_one_field(
        self,
        query_text: str,
        *,
        filters: dict[str, Any],
        limit: int,
    ) -> list[FieldEmbeddingHit]:
        results = self._store.search(
            VectorSearchQuery(
                collection=PAPER_FIELD_CHUNKS_COLLECTION,
                text=query_text,
                filters=filters,
                limit=limit,
            )
        )
        hits: list[FieldEmbeddingHit] = []
        for result in results:
            chunk_id = result.payload.get("chunk_id")
            field_name = result.payload.get("field_name")
            if not chunk_id or not field_name:
                continue
            hits.append(FieldEmbeddingHit(
                chunk_id=str(chunk_id),
                field_name=str(field_name),
                score=float(result.score),
                field_text=str(result.payload.get("field_text") or result.text or ""),
                metadata=dict(result.payload),
            ))
        return hits


def _chunk_to_field_docs(chunk: Any) -> list[VectorDocument]:
    field_texts = _extract_field_texts(chunk)
    docs: list[VectorDocument] = []
    for field_name, field_text in field_texts.non_empty().items():
        payload = _field_payload(chunk, field_name=field_name, field_text=field_text, sources=field_texts.sources_for(field_name))
        docs.append(VectorDocument(
            document_id=f"{chunk.chunk_id}:{field_name}",
            collection=PAPER_FIELD_CHUNKS_COLLECTION,
            text=field_text,
            payload=payload,
            source_type="paper_chunk_field",
            topic=chunk.paper_id,
            section_id=chunk.chunk_id,
        ))
    return docs


def _field_payload(
    chunk: Any,
    *,
    field_name: str,
    field_text: str,
    sources: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "paper_id": chunk.paper_id,
        "chunk_id": chunk.chunk_id,
        "field_name": field_name,
        "field_text": field_text,
        "field_text_sources": list(sources),
        "chunk_type": chunk.chunk_type,
        "section_title": chunk.section_title,
        "section_role": list(chunk.section_role),
        "section_index": chunk.section_index,
        "has_formula": chunk.has_formula,
        "has_figure": chunk.has_figure,
        "has_table": chunk.has_table,
        "figure_id": chunk.figure_id,
        "table_id": chunk.metadata.get("table_id", ""),
        "source_locator": chunk.metadata.get("source_locator", ""),
        "caption_source_locator": chunk.metadata.get("caption_source_locator", ""),
        "page": chunk.metadata.get("page"),
        "pdf_rect": chunk.metadata.get("pdf_rect"),
        "caption_pdf_rect": chunk.metadata.get("caption_pdf_rect"),
    }


def _extract_field_texts(chunk: Any) -> _PaperChunkFieldText:
    title, title_sources = _title_text(chunk)
    abstract, abstract_sources = _abstract_text(chunk)
    caption, caption_sources = _caption_text(chunk)
    equation, equation_sources = _equation_text(chunk)
    body, body_sources = _body_text(chunk)
    table_rows, table_rows_sources = _table_rows_text(chunk)
    table_columns, table_columns_sources = _table_columns_text(chunk)
    visual_description, visual_description_sources = _visual_description_text(chunk)
    referenced_text, referenced_text_sources = _referenced_text(chunk)
    sources = {
        "title": tuple(title_sources),
        "abstract": tuple(abstract_sources),
        "caption": tuple(caption_sources),
        "equation": tuple(equation_sources),
        "body": tuple(body_sources),
        "table_rows": tuple(table_rows_sources),
        "table_columns": tuple(table_columns_sources),
        "visual_description": tuple(visual_description_sources),
        "referenced_text": tuple(referenced_text_sources),
    }
    return _PaperChunkFieldText(
        title=title,
        abstract=abstract,
        caption=caption,
        equation=equation,
        body=body,
        table_rows=table_rows,
        table_columns=table_columns,
        visual_description=visual_description,
        referenced_text=referenced_text,
        sources={name: value for name, value in sources.items() if value},
    )


def _title_text(chunk: Any) -> tuple[str, list[str]]:
    text = _normalize_text(getattr(chunk, "section_title", ""))
    return text, ["section_title"] if text else []


def _abstract_text(chunk: Any) -> tuple[str, list[str]]:
    section_title = str(getattr(chunk, "section_title", ""))
    if getattr(chunk, "chunk_type", "") == "abstract" or section_title.casefold() == "abstract":
        text = _normalize_text(getattr(chunk, "content", ""))
        return text, ["content"] if text else []
    return "", []


def _caption_text(chunk: Any) -> tuple[str, list[str]]:
    metadata = _metadata(chunk)
    values: list[tuple[str, str]] = [
        ("metadata.caption_text", str(metadata.get("caption_text") or "")),
        ("metadata.surya_caption", str(metadata.get("surya_caption") or "")),
    ]
    visual_description = str(metadata.get("visual_description") or "")
    if visual_description:
        values.append(("metadata.visual_description", visual_description))
    content_sources = metadata.get("content_sources", [])
    if getattr(chunk, "chunk_type", "") in {"figure", "table"} or "caption" in content_sources:
        values.append(("content.caption_block", _caption_block(str(getattr(chunk, "content", "")))))
    return _join_sourced_values(values)


def _equation_text(chunk: Any) -> tuple[str, list[str]]:
    metadata = _metadata(chunk)
    chunk_type = getattr(chunk, "chunk_type", "")
    content = str(getattr(chunk, "content", ""))
    formula_latex = str(getattr(chunk, "formula_latex", "") or (content if getattr(chunk, "has_formula", False) or chunk_type == "formula" else ""))
    values = [
        ("formula_latex", formula_latex),
        ("formula_description", str(getattr(chunk, "formula_description", "") or "")),
        ("metadata.formula_normalized_latex", _metadata_list_text(metadata.get("formula_normalized_latex"))),
        ("metadata.formula_symbols", _metadata_list_text(metadata.get("formula_symbols"))),
        ("metadata.formula_operators", _metadata_list_text(metadata.get("formula_operators"))),
        ("metadata.formula_structure_tokens", _metadata_list_text(metadata.get("formula_structure_tokens"))),
        ("metadata.formula_reference_labels", _metadata_list_text(metadata.get("formula_reference_labels"))),
        ("metadata.formula_context_terms", _metadata_list_text(metadata.get("formula_context_terms"))),
        ("metadata.formula_referenced_text", _metadata_list_text(metadata.get("formula_referenced_text"))),
    ]
    if getattr(chunk, "has_formula", False) or chunk_type == "formula":
        values.append(("content", content))
    return _join_sourced_values(values)


def _body_text(chunk: Any) -> tuple[str, list[str]]:
    metadata = _metadata(chunk)
    chunk_type = getattr(chunk, "chunk_type", "")
    values = [("content", str(getattr(chunk, "content", "")))]
    if chunk_type == "table" or getattr(chunk, "has_table", False) or metadata.get("table_id"):
        values.extend([
            ("metadata.semantic_text", str(metadata.get("semantic_text") or "")),
            ("metadata.table_text", str(metadata.get("table_text") or "")),
            ("metadata.table_columns", _metadata_list_text(metadata.get("columns"))),
            ("metadata.table_rows", _metadata_rows_text(metadata.get("rows"))),
        ])
    visual_description = str(metadata.get("visual_description") or "")
    if visual_description and visual_description not in str(getattr(chunk, "content", "")):
        values.append(("metadata.visual_description", visual_description))
    return _join_sourced_values(values)


def _table_rows_text(chunk: Any) -> tuple[str, list[str]]:
    metadata = _metadata(chunk)
    if not (getattr(chunk, "chunk_type", "") == "table" or getattr(chunk, "has_table", False) or metadata.get("table_id")):
        return "", []
    values = [
        ("metadata.rows", _metadata_rows_text(metadata.get("rows"))),
        ("metadata.table_rows", _metadata_rows_text(metadata.get("table_rows"))),
    ]
    return _join_sourced_values(values)


def _table_columns_text(chunk: Any) -> tuple[str, list[str]]:
    metadata = _metadata(chunk)
    if not (getattr(chunk, "chunk_type", "") == "table" or getattr(chunk, "has_table", False) or metadata.get("table_id")):
        return "", []
    values = [
        ("metadata.columns", _metadata_list_text(metadata.get("columns"))),
        ("metadata.table_columns", _metadata_list_text(metadata.get("table_columns"))),
    ]
    return _join_sourced_values(values)


def _visual_description_text(chunk: Any) -> tuple[str, list[str]]:
    text = _normalize_text(str(_metadata(chunk).get("visual_description") or ""))
    return text, ["metadata.visual_description"] if text else []


def _referenced_text(chunk: Any) -> tuple[str, list[str]]:
    metadata = _metadata(chunk)
    values = [
        ("metadata.formula_referenced_text", _metadata_list_text(metadata.get("formula_referenced_text"))),
        ("metadata.referenced_text", _metadata_list_text(metadata.get("referenced_text"))),
        ("metadata.reference_text", _metadata_list_text(metadata.get("reference_text"))),
        ("metadata.referenced_by_chunks", _referenced_by_text(metadata.get("referenced_by_chunks"))),
    ]
    return _join_sourced_values(values)


def _metadata(chunk: Any) -> dict[str, Any]:
    metadata = getattr(chunk, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _join_sourced_values(values: list[tuple[str, str]]) -> tuple[str, list[str]]:
    parts: list[str] = []
    sources: list[str] = []
    seen_parts: set[str] = set()
    for source, raw in values:
        text = _normalize_text(raw)
        if not text or text in seen_parts:
            continue
        parts.append(text)
        sources.append(source)
        seen_parts.add(text)
    return "\n".join(parts), sources


def _caption_block(content: str) -> str:
    marker = "caption:"
    normalized = content.casefold()
    index = normalized.find(marker)
    if index < 0:
        return ""
    start = index + len(marker)
    tail = content[start:]
    lines: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if stripped.endswith(":") and lines:
            break
        lines.append(stripped)
    return " ".join(lines)


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _metadata_list_text(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if str(item).strip())
    if isinstance(value, tuple):
        return "\n".join(str(item) for item in value if str(item).strip())
    return str(value or "")


def _metadata_rows_text(value: object) -> str:
    if not isinstance(value, list):
        return str(value or "")
    rows: list[str] = []
    for row in value:
        if isinstance(row, dict):
            cells = [str(cell) for cell in row.values() if str(cell).strip()]
            if cells:
                rows.append(" | ".join(cells))
            continue
        text = str(row).strip()
        if text:
            rows.append(text)
    return "\n".join(rows)


def _referenced_by_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    texts: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("text_ref") or item.get("snippet") or "").strip()
            if text:
                texts.append(text)
    return "\n".join(texts)


def _normalized_field_names(field_names: tuple[str, ...] | None) -> tuple[str, ...]:
    if not field_names:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for field_name in field_names:
        normalized = str(field_name).strip().casefold()
        if normalized in FIELD_NAMES and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return tuple(out)


__all__ = ["PAPER_FIELD_CHUNKS_COLLECTION", "PaperFieldChunkStore"]
