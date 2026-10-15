from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.foundation import PrimitiveModel, canonicalize_url, normalize_key
from backend.research.domain.common import (
    GateResult,
    SourceLineage,
    bounded_float,
    ensure_utc,
    require_text,
    stable_research_id,
    unique_texts,
)


ResearchSourceType = Literal[
    "arxiv",
    "openreview",
    "doi",
    "crossref",
    "publisher",
    "local",
    "github",
    "manual",
    "other",
]

SourceAccessStatus = Literal[
    "available",
    "metadata_only",
    "denied",
    "rate_limited",
    "not_found",
    "unsupported",
    "failed",
    "error",
]
CatalogRelationStatus = Literal["candidate", "verified", "rejected", "conflicting"]
CatalogRelationType = Literal[
    "paper_task",
    "paper_method",
    "paper_dataset",
    "paper_benchmark",
    "paper_metric",
    "paper_score",
    "paper_code_repository",
]
CatalogTargetType = Literal[
    "task",
    "method",
    "dataset",
    "benchmark",
    "metric",
    "score",
    "code_repository",
]


def _normalized_actor_scope(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalize the small, explicit set of actor isolation dimensions."""

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


class ResearchSourceSnapshot(PrimitiveModel):
    """Immutable observation of one source used to build a research record."""

    snapshot_id: str
    paper_id: str
    source_type: ResearchSourceType = "other"
    canonical_url: str | None = None
    external_id: str | None = None
    content_type: str | None = None
    source_hash: str | None = None
    checksum: str | None = None
    fetched_at: datetime | None = None
    observed_at: datetime | None = None
    access_status: SourceAccessStatus = "available"
    reason_code: str | None = None
    retryable: bool | None = None
    user_action_required: bool | None = None
    retry_after_seconds: int | None = None
    source_policy: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    parent_snapshot_id: str | None = None
    version_id: str | None = None
    request_fingerprint: str | None = None
    resolver_version: str | None = None
    lineage: SourceLineage
    artifact_refs: list[str] = Field(default_factory=list)
    actor_scope: dict[str, str] = Field(default_factory=dict)
    schema_version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("snapshot_id", "paper_id")
    @classmethod
    def _required_ids(cls, value: str) -> str:
        return require_text(value, "source snapshot identity fields")

    @field_validator("canonical_url")
    @classmethod
    def _normalize_url(cls, value: str | None) -> str | None:
        text = value.strip() if value is not None else None
        return canonicalize_url(text) if text else None

    @field_validator(
        "external_id",
        "content_type",
        "source_hash",
        "checksum",
        "reason_code",
        "parent_snapshot_id",
        "version_id",
        "request_fingerprint",
        "resolver_version",
    )
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("actor_scope")
    @classmethod
    def _normalize_actor_scope(cls, value: Mapping[str, Any]) -> dict[str, str]:
        return _normalized_actor_scope(value)

    @field_validator("diagnostics")
    @classmethod
    def _normalize_diagnostics(cls, value: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [dict(item) for item in value if isinstance(item, Mapping)]

    @field_validator("retry_after_seconds")
    @classmethod
    def _normalize_retry_after(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or int(value) < 0:
            raise ValueError("source snapshot retry_after_seconds must be non-negative")
        return int(value)

    @field_validator("schema_version")
    @classmethod
    def _valid_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or int(value) < 1:
            raise ValueError("source snapshot schema_version must be positive")
        return int(value)

    @model_validator(mode="after")
    def _validate_locator(self) -> "ResearchSourceSnapshot":
        if not self.canonical_url and not self.external_id and not self.artifact_refs:
            raise ValueError("source snapshot requires canonical_url, external_id, or artifact_refs")
        self.lineage.require_refs("source snapshot requires source lineage")
        if self.source_hash and self.checksum and self.source_hash != self.checksum:
            raise ValueError("source_hash and checksum must match when both are provided")
        if self.source_hash and not self.checksum:
            object.__setattr__(self, "checksum", self.source_hash)
        elif self.checksum and not self.source_hash:
            object.__setattr__(self, "source_hash", self.checksum)
        object.__setattr__(self, "artifact_refs", unique_texts(self.artifact_refs))
        if self.fetched_at is not None:
            object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at))
        if self.observed_at is None:
            object.__setattr__(self, "observed_at", self.fetched_at or datetime.now(UTC))
        else:
            object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        if self.fetched_at is None:
            object.__setattr__(self, "fetched_at", self.observed_at)
        metadata = dict(self.metadata)
        for field_name in (
            "reason_code",
            "parent_snapshot_id",
            "version_id",
            "request_fingerprint",
            "resolver_version",
        ):
            if getattr(self, field_name) is None and metadata.get(field_name) is not None:
                object.__setattr__(self, field_name, str(metadata[field_name]).strip() or None)
            value = getattr(self, field_name)
            if value is not None:
                metadata[field_name] = value
        if self.retryable is None and isinstance(metadata.get("retryable"), bool):
            object.__setattr__(self, "retryable", metadata["retryable"])
        if self.user_action_required is None and isinstance(metadata.get("user_action_required"), bool):
            object.__setattr__(self, "user_action_required", metadata["user_action_required"])
        if self.retry_after_seconds is None and metadata.get("retry_after_seconds") is not None:
            try:
                object.__setattr__(self, "retry_after_seconds", max(0, int(metadata["retry_after_seconds"])))
            except (TypeError, ValueError):
                pass
        policy = metadata.get("source_policy")
        if isinstance(policy, Mapping):
            object.__setattr__(self, "source_policy", {str(key): value for key, value in policy.items()})
        if self.retryable is not None:
            metadata["retryable"] = bool(self.retryable)
        if self.user_action_required is not None:
            metadata["user_action_required"] = bool(self.user_action_required)
        if self.retry_after_seconds is not None:
            metadata["retry_after_seconds"] = int(self.retry_after_seconds)
        if self.source_policy:
            metadata["source_policy"] = dict(self.source_policy)
        if self.diagnostics:
            metadata["diagnostics"] = [dict(item) for item in self.diagnostics]
        object.__setattr__(self, "metadata", metadata)
        # Scope may arrive through different adapter generations. Merge all
        # envelopes so unrelated metadata cannot hide lineage isolation.
        scope = _normalized_actor_scope(self.lineage.metadata)
        scope.update(_normalized_actor_scope(self.metadata))
        scope.update(_normalized_actor_scope(self.actor_scope))
        object.__setattr__(self, "actor_scope", scope)
        if scope:
            object.__setattr__(self, "metadata", {**dict(self.metadata), "actor_scope": scope})
            object.__setattr__(
                self,
                "lineage",
                self.lineage.model_copy(
                    update={"metadata": {**dict(self.lineage.metadata), "actor_scope": scope}}
                ),
            )
        return self


class ResearchPaperIdentity(PrimitiveModel):
    """Canonical identity and stable external identifiers for a paper."""

    paper_id: str
    canonical_paper_id: str | None = None
    title: str = ""
    canonical_title: str | None = None
    authors: list[str] = Field(default_factory=list)
    published_year: int | None = None
    publication_year: int | None = None
    canonical_url: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    openreview_id: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_snapshot_refs: list[str] = Field(default_factory=list)
    fingerprint: str | None = None
    title_author_year_fingerprint: str | None = None
    field_provenance: dict[str, list[str]] = Field(default_factory=dict)
    external_links: dict[str, str] = Field(default_factory=dict)
    metadata_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    actor_scope: dict[str, str] = Field(default_factory=dict)
    observed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("paper_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "paper identity fields")

    @field_validator("title", "canonical_title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str | None:
        return str(value).strip() if value is not None else None

    @field_validator("authors", "versions", "source_snapshot_ids")
    @classmethod
    def _unique_values(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @field_validator("source_snapshot_refs")
    @classmethod
    def _unique_snapshot_refs(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @field_validator("metadata_conflicts")
    @classmethod
    def _normalize_conflicts(cls, value: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [dict(item) for item in value if isinstance(item, Mapping)]

    @field_validator("field_provenance")
    @classmethod
    def _normalize_field_provenance(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            str(field_name): unique_texts(list(refs))
            for field_name, refs in value.items()
            if str(field_name).strip() and refs
        }

    @field_validator("actor_scope")
    @classmethod
    def _normalize_actor_scope(cls, value: Mapping[str, Any]) -> dict[str, str]:
        return _normalized_actor_scope(value)

    @field_validator("canonical_url")
    @classmethod
    def _normalize_url(cls, value: str | None) -> str | None:
        text = value.strip() if value is not None else None
        return canonicalize_url(text) if text else None

    @model_validator(mode="after")
    def _derive_fingerprint(self) -> "ResearchPaperIdentity":
        title = self.title or self.canonical_title or ""
        if not title.strip():
            raise ValueError("paper identity fields title is required")
        title = title.strip()
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "canonical_title", self.canonical_title or title)
        published_year = self.published_year if self.published_year is not None else self.publication_year
        object.__setattr__(self, "published_year", published_year)
        object.__setattr__(self, "publication_year", published_year)
        snapshot_refs = unique_texts([*self.source_snapshot_ids, *self.source_snapshot_refs])
        object.__setattr__(self, "source_snapshot_ids", snapshot_refs)
        object.__setattr__(self, "source_snapshot_refs", snapshot_refs)
        if self.canonical_paper_id and self.canonical_paper_id != self.paper_id:
            raise ValueError("canonical_paper_id must match paper_id")
        object.__setattr__(self, "canonical_paper_id", self.paper_id)
        fallback_fingerprint = build_paper_identity_fingerprint(
            title,
            self.authors,
            published_year,
        )
        fingerprint = self.fingerprint or self.title_author_year_fingerprint or fallback_fingerprint
        title_fingerprint = self.title_author_year_fingerprint or fallback_fingerprint
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "title_author_year_fingerprint", title_fingerprint)
        conflicts = list(self.metadata_conflicts)
        metadata_conflicts = self.metadata.get("metadata_conflicts") or self.metadata.get("conflict_diagnostics")
        if isinstance(metadata_conflicts, list):
            conflicts.extend(dict(item) for item in metadata_conflicts if isinstance(item, Mapping))
        object.__setattr__(self, "metadata_conflicts", conflicts)
        if conflicts:
            object.__setattr__(self, "metadata", {**dict(self.metadata), "metadata_conflicts": conflicts})
        links = self.external_links
        if not isinstance(links, Mapping):
            links = {}
        object.__setattr__(self, "external_links", {str(key): str(value) for key, value in links.items() if str(value).strip()})
        scope = _normalized_actor_scope(self.metadata)
        scope.update(_normalized_actor_scope(self.actor_scope))
        object.__setattr__(self, "actor_scope", scope)
        if scope:
            object.__setattr__(self, "metadata", {**dict(self.metadata), "actor_scope": scope})
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        return self


class ResearchPaperRelation(PrimitiveModel):
    relation_id: str
    paper_id: str
    relation_type: CatalogRelationType
    target_type: CatalogTargetType
    target_id: str = ""
    target_ref: str | None = None
    status: CatalogRelationStatus = "candidate"
    confidence: float = 0.0
    source_snapshot_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    observed_at: datetime | None = None
    observed_by: str | None = None
    actor_scope: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("relation_id", "paper_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "paper relation fields")

    @field_validator("target_id", "target_ref")
    @classmethod
    def _normalize_target(cls, value: str | None) -> str | None:
        return str(value).strip() if value is not None else None

    @field_validator("confidence")
    @classmethod
    def _bounded_confidence(cls, value: float) -> float:
        return bounded_float(value, "relation confidence")

    @field_validator("source_snapshot_refs", "evidence_refs")
    @classmethod
    def _normalize_refs(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @field_validator("actor_scope")
    @classmethod
    def _normalize_actor_scope(cls, value: Mapping[str, Any]) -> dict[str, str]:
        return _normalized_actor_scope(value)

    @model_validator(mode="after")
    def _validate_relation(self) -> "ResearchPaperRelation":
        target_id = self.target_id or self.target_ref
        if not target_id:
            raise ValueError("paper relation target_id or target_ref is required")
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "target_ref", self.target_ref or target_id)
        expected = {
            "paper_task": "task",
            "paper_method": "method",
            "paper_dataset": "dataset",
            "paper_benchmark": "benchmark",
            "paper_metric": "metric",
            "paper_score": "score",
            "paper_code_repository": "code_repository",
        }[self.relation_type]
        if self.target_type != expected:
            raise ValueError(f"{self.relation_type} requires target_type={expected}")
        if self.status == "verified" and (not self.source_snapshot_refs or not self.evidence_refs):
            raise ValueError("verified paper relation requires source and evidence refs")
        if not self.source_snapshot_refs:
            raise ValueError("paper relation requires source snapshot refs")
        for field_name in ("created_at", "observed_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))
        scope = _normalized_actor_scope(self.metadata)
        scope.update(_normalized_actor_scope(self.actor_scope))
        object.__setattr__(self, "actor_scope", scope)
        if scope:
            object.__setattr__(self, "metadata", {**dict(self.metadata), "actor_scope": scope})
        return self


class ResearchPaperCatalogEntry(PrimitiveModel):
    entry_id: str
    paper_id: str
    identity: ResearchPaperIdentity
    relations: list[ResearchPaperRelation] = Field(default_factory=list)
    status: Literal["catalog_partial", "catalog_ready"] = "catalog_partial"
    source_snapshot_refs: list[str] = Field(default_factory=list)
    identity_ref: str | None = None
    relation_refs: list[str] = Field(default_factory=list)
    evidence_coverage: dict[str, float] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    observed_at: datetime | None = None
    last_refresh_run_id: str | None = None
    actor_scope: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entry_id", "paper_id")
    @classmethod
    def _required_ids(cls, value: str) -> str:
        return require_text(value, "catalog entry identity fields")

    @field_validator("source_snapshot_refs")
    @classmethod
    def _unique_source_refs(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @field_validator("relation_refs")
    @classmethod
    def _unique_relation_refs(cls, value: list[str]) -> list[str]:
        return unique_texts(value)

    @field_validator("evidence_coverage")
    @classmethod
    def _normalize_evidence_coverage(cls, value: Mapping[str, Any]) -> dict[str, float]:
        result: dict[str, float] = {}
        for key, raw in value.items():
            number = float(raw)
            if number < 0.0 or number > 1.0:
                raise ValueError("evidence coverage values must be between 0 and 1")
            result[str(key)] = number
        return result

    @field_validator("actor_scope")
    @classmethod
    def _normalize_actor_scope(cls, value: Mapping[str, Any]) -> dict[str, str]:
        return _normalized_actor_scope(value)

    @model_validator(mode="after")
    def _validate_entry(self) -> "ResearchPaperCatalogEntry":
        if self.identity.paper_id != self.paper_id:
            raise ValueError("catalog identity must match paper_id")
        seen: set[str] = set()
        deduped: list[ResearchPaperRelation] = []
        for relation in self.relations:
            if relation.paper_id != self.paper_id:
                raise ValueError("catalog relations must match paper_id")
            if relation.relation_id not in seen:
                seen.add(relation.relation_id)
                deduped.append(relation)
        object.__setattr__(self, "relations", deduped)
        if self.identity_ref is None:
            object.__setattr__(self, "identity_ref", stable_identity_ref(self.identity))
        if not self.relation_refs:
            object.__setattr__(self, "relation_refs", [relation.relation_id for relation in deduped])
        if not self.evidence_coverage:
            total = len(deduped)
            with_evidence = sum(bool(relation.evidence_refs) for relation in deduped)
            object.__setattr__(
                self,
                "evidence_coverage",
                {"relations": (with_evidence / total if total else 0.0)},
            )
        for field_name in ("created_at", "updated_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))
        if self.observed_at is None:
            object.__setattr__(self, "observed_at", self.updated_at or self.created_at)
        elif self.observed_at is not None:
            object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        scope = _normalized_actor_scope(self.metadata)
        scope.update(_normalized_actor_scope(self.actor_scope))
        object.__setattr__(self, "actor_scope", scope)
        if scope:
            object.__setattr__(self, "metadata", {**dict(self.metadata), "actor_scope": scope})
        return self


def build_paper_identity_fingerprint(
    title: str,
    authors: list[str] | tuple[str, ...] = (),
    published_year: int | None = None,
) -> str:
    """Build a deterministic fallback key for identity matching."""

    normalized_title = normalize_key(require_text(title, "paper title"))
    normalized_authors = ",".join(sorted(normalize_key(author) for author in authors if str(author).strip()))
    year = str(published_year or "")
    return "|".join(part for part in (normalized_title, normalized_authors, year) if part)


def build_local_content_paper_id(
    source_hash: str,
    actor_scope: Mapping[str, Any] | None,
) -> str:
    """Build an owner-scoped identity for verified local document bytes.

    Local paths, upload request ids, and filenames are observations used for
    provenance. They must not create a new paper identity when the same owner
    imports the same bytes again. The actor scope remains part of the identity
    so local content identities never cross an authorization boundary.
    """

    digest = str(source_hash or "").strip().casefold()
    if digest.startswith("sha256:"):
        digest = digest.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("local paper source_hash must be a SHA-256 digest")
    return stable_research_id(
        "paper",
        "local-content-sha256",
        actor_scope_ref(actor_scope),
        digest,
    )


def stable_identity_ref(identity: ResearchPaperIdentity) -> str:
    """Return a stable typed reference for a canonical paper identity."""

    return f"identity://{identity.paper_id}"


def same_paper_identity(left: ResearchPaperIdentity, right: ResearchPaperIdentity) -> bool:
    """Compare identities using external IDs, canonical URL, then fingerprint."""

    for left_value, right_value in (
        (left.arxiv_id, right.arxiv_id),
        (left.doi, right.doi),
        (left.openreview_id, right.openreview_id),
        (left.canonical_url, right.canonical_url),
        (left.fingerprint, right.fingerprint),
    ):
        if left_value and right_value and normalize_key(left_value) == normalize_key(right_value):
            return True
    return False


def validate_relation_for_publication(relation: ResearchPaperRelation) -> GateResult:
    """A relation can be published as verified only with complete lineage."""

    if relation.status != "verified":
        return GateResult.fail(
            "CatalogRelationVerificationGate",
            "catalog relation is not verified",
            metadata={"status": relation.status},
        )
    if not relation.source_snapshot_refs or not relation.evidence_refs:
        return GateResult.fail(
            "CatalogRelationEvidenceGate",
            "verified catalog relation requires source and evidence refs",
        )
    return GateResult.pass_("CatalogRelationVerificationGate")


def metric_compatibility_key(
    *,
    dataset_id: str,
    dataset_version: str | None,
    metric_id: str,
    metric_direction: str,
    metric_unit: str | None,
    split: str | None,
    evaluation_protocol: str | None,
    protocol_fingerprint: str | None = None,
) -> tuple[str, ...]:
    """Return the normalized fields required for deterministic score comparison."""

    def _dimension(value: str | None) -> str:
        return str(value or "").strip().casefold()

    base = (
        normalize_key(dataset_id),
        _dimension(dataset_version),
        normalize_key(metric_id),
        _dimension(metric_direction),
        _dimension(metric_unit),
        _dimension(split),
        _dimension(evaluation_protocol),
    )
    return base if protocol_fingerprint is None else (*base, _dimension(protocol_fingerprint))


def metric_dimensions_compatible(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Compare normalized dataset/metric dimensions without comparing score values."""

    return tuple(left) == tuple(right)


def actor_scope_ref(scope: Mapping[str, Any] | None) -> str:
    """Return a stable, non-secret key for a tenant/user/memory scope."""

    values = scope or {}
    return "|".join(
        f"{key}={str(values.get(key) or '').strip()}"
        for key in ("tenant_id", "user_id", "memory_namespace")
        if str(values.get(key) or "").strip()
    ) or "public"


def actor_scope_matches(
    persisted: Mapping[str, Any] | None,
    requested: Mapping[str, Any] | None,
) -> bool:
    """Check visibility without allowing a caller to widen a stored scope."""

    stored = _scope_dimensions(persisted)
    wanted = _scope_dimensions(requested)
    if not stored:
        # Legacy records without scope metadata are public and remain readable.
        return True
    return all(wanted.get(key) == value for key, value in stored.items())


def _scope_dimensions(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Read flat or metadata-envelope actor scopes consistently."""

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
    "CatalogRelationStatus",
    "CatalogRelationType",
    "CatalogTargetType",
    "ResearchPaperCatalogEntry",
    "ResearchPaperIdentity",
    "ResearchPaperRelation",
    "ResearchSourceSnapshot",
    "ResearchSourceType",
    "SourceAccessStatus",
    "build_local_content_paper_id",
    "build_paper_identity_fingerprint",
    "stable_identity_ref",
    "actor_scope_matches",
    "actor_scope_ref",
    "metric_compatibility_key",
    "metric_dimensions_compatible",
    "same_paper_identity",
    "validate_relation_for_publication",
]
