from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.foundation import PrimitiveModel
from backend.research.domain.document_fidelity import DocumentFidelity, TableGrid
from backend.research.domain.common import (
    SourceLineage,
    ensure_utc,
    require_text,
    stable_research_id,
    unique_texts,
)


class ResearchSection(PrimitiveModel):
    section_id: str
    title: str
    level: int = 1
    text: str = ""
    page_start: int | None = None
    page_end: int | None = None
    source_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("section_id", "title", "source_ref")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "section fields")

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ResearchSection":
        if self.level < 1:
            raise ValueError("section level must be greater than zero")
        if self.page_start is not None and self.page_start < 0:
            raise ValueError("page_start must be non-negative")
        if self.page_end is not None and self.page_start is not None and self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        return self


class ResearchFigure(PrimitiveModel):
    figure_id: str
    caption: str
    source_ref: str
    image_ref: str | None = None
    page: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("figure_id", "caption", "source_ref")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "figure fields")


class ResearchTable(PrimitiveModel):
    table_id: str
    caption: str
    source_ref: str
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    page: int | None = None
    grid: TableGrid | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("table_id", "caption", "source_ref")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "table fields")

    @field_validator("columns")
    @classmethod
    def _unique_columns(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @model_validator(mode="after")
    def _validate_grid_identity(self) -> "ResearchTable":
        if self.grid is not None and self.grid.table_id != self.table_id:
            raise ValueError("table grid identity must match its ResearchTable")
        return self


class ResearchEquation(PrimitiveModel):
    equation_id: str
    latex: str
    source_ref: str
    page: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("equation_id", "latex", "source_ref")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "equation fields")


class ResearchReference(PrimitiveModel):
    reference_id: str
    title: str
    source_ref: str
    authors: list[str] = Field(default_factory=list)
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reference_id", "title", "source_ref")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "reference fields")

    @field_validator("authors")
    @classmethod
    def _unique_authors(cls, value: list[str]) -> list[str]:
        return unique_texts(value)


class ResearchDocument(PrimitiveModel):
    paper_id: str
    source_hash: str
    document_id: str | None = None
    source_snapshot_id: str | None = None
    title: str | None = None
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    parser_backend: str | None = None
    parser_version: str | None = None
    normalization_version: str | None = None
    language: str | None = None
    sections: list[ResearchSection] = Field(default_factory=list)
    figures: list[ResearchFigure] = Field(default_factory=list)
    tables: list[ResearchTable] = Field(default_factory=list)
    equations: list[ResearchEquation] = Field(default_factory=list)
    references: list[ResearchReference] = Field(default_factory=list)
    lineage: SourceLineage
    artifact_refs: list[str] = Field(default_factory=list)
    parser_attempts: list[dict[str, Any]] = Field(default_factory=list)
    quality_report: dict[str, Any] = Field(default_factory=dict)
    fidelity: DocumentFidelity | None = None
    status: Literal["parsed", "degraded", "quarantined"] = "parsed"
    source_locators: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    observed_at: datetime | None = None
    actor_scope: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("paper_id", "source_hash")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "document fields")

    @field_validator("authors")
    @classmethod
    def _unique_authors(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @field_validator("artifact_refs", "source_locators")
    @classmethod
    def _unique_refs(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @field_validator("source_snapshot_id", "title", "abstract", "parser_backend", "parser_version", "normalization_version", "language")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("actor_scope")
    @classmethod
    def _normalize_actor_scope(cls, value: Mapping[str, Any]) -> dict[str, str]:
        return _normalized_actor_scope(value)

    @model_validator(mode="after")
    def _require_lineage(self) -> "ResearchDocument":
        self.lineage.require_refs("research document requires source lineage")
        metadata = dict(self.metadata)
        lineage_metadata = dict(self.lineage.metadata)
        source_snapshot_id = self.source_snapshot_id or _first_text(
            metadata.get("source_snapshot_id"),
            lineage_metadata.get("source_snapshot_id"),
        )
        if source_snapshot_id:
            object.__setattr__(self, "source_snapshot_id", source_snapshot_id)
            metadata["source_snapshot_id"] = source_snapshot_id
        # Parser adapters predate the typed fields and write these values in
        # metadata. Promote them while retaining the original metadata keys.
        for field_name, aliases in {
            "parser_backend": ("parser_backend", "backend", "compiler"),
            "parser_version": ("parser_version",),
            "normalization_version": ("normalization_version", "normalizer_version"),
            "language": ("language", "lang"),
            "title": ("title", "document_title", "html_title"),
            "abstract": ("abstract",),
        }.items():
            current = getattr(self, field_name)
            if current is None:
                for alias in aliases:
                    candidate = _first_text(metadata.get(alias), lineage_metadata.get(alias))
                    if candidate:
                        object.__setattr__(self, field_name, candidate)
                        break
        if not self.authors:
            raw_authors = metadata.get("authors")
            if isinstance(raw_authors, (list, tuple)):
                object.__setattr__(self, "authors", unique_texts([str(item) for item in raw_authors]))
        parser_attempts = self.parser_attempts or metadata.get("parser_attempts") or metadata.get("compiler_attempts")
        if isinstance(parser_attempts, list):
            object.__setattr__(self, "parser_attempts", [dict(item) for item in parser_attempts if isinstance(item, Mapping)])
        quality_report = self.quality_report or metadata.get("quality_report") or metadata.get("parse_quality")
        if isinstance(quality_report, Mapping):
            object.__setattr__(self, "quality_report", dict(quality_report))
        if metadata.get("degraded") and self.status == "parsed":
            object.__setattr__(self, "status", "degraded")
        source_locators = list(self.source_locators)
        for item in (*self.sections, *self.figures, *self.tables, *self.equations, *self.references):
            source_ref = getattr(item, "source_ref", None)
            if source_ref:
                source_locators.append(str(source_ref))
        object.__setattr__(self, "source_locators", unique_texts(source_locators))
        object.__setattr__(self, "artifact_refs", unique_texts([*self.artifact_refs, *self.lineage.artifact_refs]))
        # Parser adapters may place scope in lineage while adding unrelated
        # parser metadata. Merge each envelope and let typed scope win.
        scope = _normalized_actor_scope(lineage_metadata)
        scope.update(_normalized_actor_scope(metadata))
        scope.update(_normalized_actor_scope(self.actor_scope))
        object.__setattr__(self, "actor_scope", scope)
        if scope:
            metadata["actor_scope"] = scope
            lineage_metadata["actor_scope"] = scope
        if self.source_hash and self.lineage.source_hash and self.source_hash != self.lineage.source_hash:
            raise ValueError("document source_hash and lineage source_hash must match")
        if self.lineage.source_hash is None:
            object.__setattr__(self, "lineage", self.lineage.model_copy(update={"source_hash": self.source_hash, "metadata": lineage_metadata}))
        elif lineage_metadata != self.lineage.metadata:
            object.__setattr__(self, "lineage", self.lineage.model_copy(update={"metadata": lineage_metadata}))
        object.__setattr__(self, "metadata", metadata)
        if not self.document_id:
            object.__setattr__(
                self,
                "document_id",
                stable_research_id("research_document", self.paper_id, self.source_hash),
            )
        for field_name in ("created_at", "observed_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))
        return self


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalized_actor_scope(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    source: dict[str, Any] = dict(value)
    nested = source.get("actor_scope")
    if isinstance(nested, Mapping):
        source.update(dict(nested))
    allowed = {"tenant_id", "user_id", "memory_namespace"}
    return {
        str(key): str(raw).strip()
        for key, raw in source.items()
        if str(key) in allowed and str(raw).strip()
    }


__all__ = [
    "ResearchDocument",
    "ResearchEquation",
    "ResearchFigure",
    "ResearchReference",
    "ResearchSection",
    "ResearchTable",
]
