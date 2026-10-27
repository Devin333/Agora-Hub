from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from backend.research.ports.catalog_models import ResearchScore, ResearchSOTAClaim
from backend.research.domain.catalog import (
    ResearchPaperCatalogEntry,
    ResearchPaperIdentity,
    ResearchPaperRelation,
    ResearchSourceSnapshot,
    actor_scope_matches,
    actor_scope_ref,
)
from backend.research.domain.code_repository import CodeRepositoryProfile
from backend.research.domain.document import ResearchDocument
from backend.research.domain.evidence import ResearchEvidencePack
from backend.research.domain.paper import ResearchPaper
from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from infrastructure.storage.json_file_store import (
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)


CATALOG_STORE_SCHEMA_VERSION = 2
_LEGACY_CATALOG_STORE_SCHEMA_VERSIONS = frozenset({1})
RESTORE_PROVENANCE_SCHEMA = "newsroom.research-catalog-restore-provenance/v1"
_RESTORE_PROVENANCE_DIR = "_restore-provenance"


class ResearchCatalogStoreError(RuntimeError):
    pass


class ResearchCatalogStoreCorruptionError(ResearchCatalogStoreError):
    pass


class ResearchCatalogArtifactNotFoundError(ResearchCatalogStoreError, FileNotFoundError):
    pass


class ResearchCatalogArtifactScopeError(ResearchCatalogStoreError, PermissionError):
    pass


class FilesystemResearchCatalogStore:
    """Atomic actor-scoped JSON persistence for Research paper intelligence."""

    def __init__(
        self,
        root: str | Path = ".newsroom/research_catalog",
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        memory_namespace: str | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.tenant_id = _optional_text(tenant_id) or "public"
        self.user_id = _optional_text(user_id) or "public"
        self.memory_namespace = _optional_text(memory_namespace) or f"research:tenant:{self.tenant_id}:user:{self.user_id}"
        scope_key = hashlib.sha256(self.scope_ref.encode("utf-8")).hexdigest()[:32]
        self.path = self.root / f"{scope_key}.json"
        self.artifact_root = self.root / "artifacts"
        self.event_root = self.root / "events"

    @property
    def scope_ref(self) -> str:
        return f"tenant={self.tenant_id}|user={self.user_id}|memory={self.memory_namespace}"

    def get(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchPaperCatalogEntry | None:
        state = self._read()
        raw = state["catalog"].get(_scoped_key(paper_id, actor_scope))
        if raw is None and actor_scope is None:
            raw = state["catalog"].get(str(paper_id))
        entry = _model(ResearchPaperCatalogEntry, raw)
        return entry if entry is None or actor_scope_matches(_value_scope(entry), actor_scope) else None

    def save(self, value: Any) -> None:
        if isinstance(value, ResearchPaperCatalogEntry):
            self._mutate(lambda state: state["catalog"].__setitem__(_scoped_key(value.paper_id, value.metadata.get("actor_scope")), _dump(value)))
            return
        if isinstance(value, ResearchPaperIdentity):
            key = _scoped_key(value.paper_id, value.metadata.get("actor_scope"))
            self._mutate(lambda state: _put_identity(state, key, value))
            return
        if isinstance(value, ResearchPaperRelation):
            self._mutate(lambda state: state["relations"].__setitem__(_scoped_key(value.relation_id, value.metadata.get("actor_scope")), _dump(value)))
            return
        if isinstance(value, ResearchSourceSnapshot):
            key = _scoped_key(value.snapshot_id, _snapshot_scope(value))
            self._mutate(lambda state: _put_immutable_snapshot(state, key, value))
            return
        if isinstance(value, ResearchPaper):
            self._mutate(lambda state: state["papers"].__setitem__(_scoped_key(value.paper_id, value.metadata), _dump(value)))
            return
        if isinstance(value, ResearchDocument):
            self._mutate(lambda state: state["documents"].__setitem__(_scoped_key(value.paper_id, value.lineage.metadata), _dump(value)))
            return
        if isinstance(value, ResearchEvidencePack):
            self._mutate(lambda state: state["evidence"].__setitem__(_scoped_key(value.paper_id, value.metadata), _dump(value)))
            return
        if isinstance(value, CodeRepositoryProfile):
            key = str(value.canonical_repo_id or value.repo_url)
            self._mutate(lambda state: state["code_profiles"].__setitem__(_scoped_key(key, value.metadata), _dump(value)))
            return
        if isinstance(value, ResearchScore):
            self.save_score(value)
            return
        if isinstance(value, ResearchSOTAClaim):
            self.save_sota_claim(value)
            return
        raise TypeError(f"unsupported catalog value: {type(value).__name__}")

    def search(self, query: str = "", *, limit: int = 50, actor_scope: Mapping[str, str] | None = None) -> list[ResearchPaperCatalogEntry]:
        needle = str(query or "").strip().casefold()
        values = [_model(ResearchPaperCatalogEntry, raw) for raw in self._read()["catalog"].values()]
        values = [value for value in values if value is not None and actor_scope_matches(_value_scope(value), actor_scope)]
        if needle:
            values = [value for value in values if needle in value.paper_id.casefold() or needle in value.identity.title.casefold()]
        return values[: int(limit)]

    def find_by_external_id(self, external_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchPaperIdentity | None:
        needle = str(external_id or "").strip().casefold()
        for raw in self._read()["identities"].values():
            identity = _model(ResearchPaperIdentity, raw)
            if identity is None:
                continue
            values = (identity.arxiv_id, identity.doi, identity.openreview_id, identity.canonical_url)
            if actor_scope_matches(_value_scope(identity), actor_scope) and any(value and str(value).strip().casefold() == needle for value in values):
                return identity
        return None

    def find_by_fingerprint(self, fingerprint: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchPaperIdentity | None:
        needle = str(fingerprint or "").strip().casefold()
        if not needle:
            return None
        for raw in self._read()["identities"].values():
            identity = _model(ResearchPaperIdentity, raw)
            if identity is not None and identity.fingerprint and identity.fingerprint.casefold() == needle and actor_scope_matches(_value_scope(identity), actor_scope):
                return identity
        return None

    def get_identity(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchPaperIdentity | None:
        state = self._read()
        raw = state["identities"].get(_scoped_key(paper_id, actor_scope))
        if raw is None and actor_scope is None:
            raw = state["identities"].get(str(paper_id))
        value = _model(ResearchPaperIdentity, raw)
        return value if value is None or actor_scope_matches(_value_scope(value), actor_scope) else None

    def get_paper(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchPaper | None:
        state = self._read()
        raw = state["papers"].get(_scoped_key(paper_id, actor_scope))
        if raw is None and actor_scope is None:
            raw = state["papers"].get(str(paper_id))
        value = _model(ResearchPaper, raw)
        return value if value is None or actor_scope_matches(_value_scope(value), actor_scope) else None

    def get_snapshot(self, snapshot_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchSourceSnapshot | None:
        state = self._read()
        raw = state["snapshots"].get(_scoped_key(snapshot_id, actor_scope))
        if raw is None and actor_scope is None:
            raw = state["snapshots"].get(str(snapshot_id))
        value = _model(ResearchSourceSnapshot, raw)
        return value if value is None or actor_scope_matches(_value_scope(value), actor_scope) else None

    def list_for_paper(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> list[ResearchSourceSnapshot]:
        return [
            snapshot
            for raw in self._read()["snapshots"].values()
            if (snapshot := _model(ResearchSourceSnapshot, raw)) is not None and snapshot.paper_id == paper_id and actor_scope_matches(_value_scope(snapshot), actor_scope)
        ]

    def list_relations(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> list[ResearchPaperRelation]:
        return [
            relation
            for raw in self._read()["relations"].values()
            if (relation := _model(ResearchPaperRelation, raw)) is not None and relation.paper_id == paper_id and actor_scope_matches(_value_scope(relation), actor_scope)
        ]

    def get_document(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchDocument | None:
        state = self._read()
        raw = state["documents"].get(_scoped_key(paper_id, actor_scope))
        if raw is None and actor_scope is None:
            raw = state["documents"].get(str(paper_id))
        value = _model(ResearchDocument, raw)
        return value if value is None or actor_scope_matches(_value_scope(value), actor_scope) else None

    def get_evidence(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchEvidencePack | None:
        state = self._read()
        raw = state["evidence"].get(_scoped_key(paper_id, actor_scope))
        if raw is None and actor_scope is None:
            raw = state["evidence"].get(str(paper_id))
        value = _model(ResearchEvidencePack, raw)
        return value if value is None or actor_scope_matches(_value_scope(value), actor_scope) else None

    def save_score(self, score: ResearchScore) -> None:
        self._mutate(lambda state: state["scores"].__setitem__(_scoped_key(score.score_id, score.metadata.get("actor_scope")), _dump(score)))

    def list_scores(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> list[ResearchScore]:
        return [
            score
            for raw in self._read()["scores"].values()
            if (score := _model(ResearchScore, raw)) is not None and score.paper_id == paper_id and actor_scope_matches(_value_scope(score), actor_scope)
        ]

    def list_all_scores(self, *, actor_scope: Mapping[str, str] | None = None) -> list[ResearchScore]:
        return [score for raw in self._read()["scores"].values() if (score := _model(ResearchScore, raw)) is not None and actor_scope_matches(_value_scope(score), actor_scope)]

    def save_sota_claim(self, claim: ResearchSOTAClaim) -> None:
        self._mutate(
            lambda state: state["sota_claims"].__setitem__(
                _scoped_key(claim.claim_id, claim.metadata.get("actor_scope")),
                _dump(claim),
            )
        )

    def list_sota_claims(
        self,
        paper_id: str,
        *,
        actor_scope: Mapping[str, str] | None = None,
    ) -> list[ResearchSOTAClaim]:
        return [
            claim
            for raw in self._read()["sota_claims"].values()
            if (claim := _model(ResearchSOTAClaim, raw)) is not None
            and claim.paper_id == paper_id
            and actor_scope_matches(_value_scope(claim), actor_scope)
        ]

    def save_code_profile(self, profile: CodeRepositoryProfile) -> None:
        self.save(profile)

    def list_code_profiles(
        self,
        paper_id: str | None = None,
        *,
        actor_scope: Mapping[str, str] | None = None,
    ) -> list[CodeRepositoryProfile]:
        # Profiles are related to papers through relation metadata. A caller
        # with no paper filter gets all observations in this actor scope.
        return [
            profile
            for raw in self._read()["code_profiles"].values()
            if (profile := _model(CodeRepositoryProfile, raw)) is not None
            and (paper_id is None or profile.metadata.get("paper_id") == paper_id)
            and actor_scope_matches(_value_scope(profile), actor_scope)
        ]

    def _read(self) -> dict[str, Any]:
        with locked_json_file(self.path) as resolved:
            return self._read_unlocked(resolved)

    def _read_unlocked(self, path: Path) -> dict[str, Any]:
        default = _empty_state(self.scope_ref)
        payload = read_json_object_unlocked(path, default=default, strict=False)
        if payload == default and not path.exists():
            return payload
        schema_version = payload.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in ({CATALOG_STORE_SCHEMA_VERSION} | _LEGACY_CATALOG_STORE_SCHEMA_VERSIONS)
            or payload.get("scope_ref") != self.scope_ref
        ):
            raise ResearchCatalogStoreCorruptionError("research catalog store identity or schema is invalid")
        checksum = payload.get("checksum")
        unsigned = {key: value for key, value in payload.items() if key != "checksum"}
        if not isinstance(checksum, str) or checksum != _checksum(unsigned):
            raise ResearchCatalogStoreCorruptionError("research catalog store checksum mismatch")
        # Files written before durable SOTA claims were introduced do not have
        # that collection. Validate their original checksum first, then add an
        # empty collection, bump the schema, and persist the migrated shape
        # atomically while the file lock is held.
        missing = [key for key in _STATE_COLLECTIONS if key not in payload]
        migrated = False
        if schema_version in _LEGACY_CATALOG_STORE_SCHEMA_VERSIONS and missing in ([], ["sota_claims"]):
            payload.setdefault("sota_claims", {})
            payload["schema_version"] = CATALOG_STORE_SCHEMA_VERSION
            migrated = True
        elif schema_version == CATALOG_STORE_SCHEMA_VERSION and missing == ["sota_claims"]:
            # Be tolerant of an interrupted v2 migration that wrote the
            # version before the collection, while still rejecting all other
            # missing collections.
            payload["sota_claims"] = {}
            migrated = True
        elif missing:
            raise ResearchCatalogStoreCorruptionError(
                f"research catalog collection is missing: {missing[0]}"
            )
        for key in _STATE_COLLECTIONS:
            if not isinstance(payload.get(key), dict):
                raise ResearchCatalogStoreCorruptionError(f"research catalog collection is invalid: {key}")
        if migrated:
            unsigned = {key: value for key, value in payload.items() if key != "checksum"}
            payload["checksum"] = _checksum(unsigned)
            write_json_object_unlocked(path, payload)
        return payload

    def _mutate(self, callback) -> None:
        with locked_json_file(self.path) as resolved:
            state = self._read_unlocked(resolved)
            callback(state)
            unsigned = {key: state[key] for key in state if key != "checksum"}
            write_json_object_unlocked(resolved, {**unsigned, "checksum": _checksum(unsigned)})

    def publish(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Publish an immutable JSON artifact and return a stable ref."""

        artifact_type = _safe_segment(artifact_type, "artifact_type")
        artifact_metadata = dict(metadata or {})
        # The store owns its scope identity; callers may add diagnostics but
        # cannot spoof the durable store namespace. For the shared production
        # store, the actor scope is the artifact namespace; a store-level
        # scope remains the fallback for legacy/public callers.
        artifact_scope = _normalized_scope(artifact_metadata.get("actor_scope"))
        artifact_metadata["scope_ref"] = (
            actor_scope_ref(artifact_scope) if artifact_scope else self.scope_ref
        )
        stored_payload = _portable_artifact_payload(
            artifact_type=artifact_type,
            payload=payload,
            metadata=artifact_metadata,
            root=self.root,
        )
        envelope = {
            "artifact_type": artifact_type,
            "metadata": artifact_metadata,
            "payload": stored_payload,
        }
        encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        target = self.artifact_root / f"{artifact_type}-{digest[:32]}.json"
        with locked_json_file(target) as resolved:
            if resolved.exists():
                existing = read_json_object_unlocked(resolved, default={}, strict=True)
                if existing != envelope:
                    raise ResearchCatalogStoreError("immutable research artifact ref conflicts with existing content")
            else:
                write_json_object_unlocked(resolved, envelope)
        marker = {
            "schema_version": 1,
            "record_type": "research_artifact_commit_marker",
            "status": "committed",
            "artifact_ref": f"artifact://research/{artifact_type}/{digest}",
            "artifact_type": artifact_type,
            "run_id": artifact_metadata.get("run_id"),
            "content_checksum": digest,
            "created_at": datetime.now(UTC).isoformat(),
            "actor_scope": artifact_metadata.get("actor_scope", {}),
        }
        marker_path = target.with_suffix(".commit.json")
        with locked_json_file(marker_path) as resolved:
            if resolved.exists():
                existing = read_json_object_unlocked(resolved, default={}, strict=True)
                if existing.get("content_checksum") != digest:
                    raise ResearchCatalogStoreError("research artifact commit marker conflicts with content")
            else:
                write_json_object_unlocked(resolved, marker)
        return f"artifact://research/{artifact_type}/{digest}"

    def read(
        self,
        ref: str,
        *,
        actor_scope: Mapping[str, str],
        include_payload: bool = False,
        max_chars: int = 200_000,
    ) -> dict[str, Any]:
        """Read one committed artifact with a fresh scope and integrity check."""

        if isinstance(include_payload, bool) is False:
            raise ValueError("include_payload must be boolean")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 1_000_000:
            raise ValueError("max_chars must be between 1 and 1000000")
        parsed = urlsplit(str(ref or "").strip())
        if parsed.scheme != "artifact" or parsed.netloc != "research":
            raise ResearchCatalogArtifactNotFoundError("research artifact reference is invalid")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            raise ResearchCatalogArtifactNotFoundError("research artifact reference is invalid")
        artifact_type = _safe_segment(parts[0], "artifact_type")
        digest = parts[1]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.casefold()):
            raise ResearchCatalogArtifactNotFoundError("research artifact reference is invalid")
        target = self.artifact_root / f"{artifact_type}-{digest[:32]}.json"
        marker_path = target.with_suffix(".commit.json")
        with locked_json_file(target) as resolved:
            if not resolved.exists():
                raise ResearchCatalogArtifactNotFoundError("research artifact was not found")
            envelope = read_json_object_unlocked(resolved, default={}, strict=True)
        if not isinstance(envelope, dict):
            raise ResearchCatalogStoreCorruptionError("research artifact envelope is invalid")
        encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ResearchCatalogStoreCorruptionError("research artifact checksum mismatch")
        if envelope.get("artifact_type") != artifact_type:
            raise ResearchCatalogStoreCorruptionError("research artifact type mismatch")
        metadata = envelope.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ResearchCatalogStoreCorruptionError("research artifact metadata is invalid")
        requested_scope = _normalized_scope(actor_scope)
        persisted_scope = _normalized_scope(metadata.get("actor_scope"))
        expected_scope_ref = actor_scope_ref(persisted_scope) if persisted_scope else self.scope_ref
        if str(metadata.get("scope_ref") or "") != expected_scope_ref:
            raise ResearchCatalogStoreCorruptionError("research artifact scope metadata is invalid")
        if requested_scope and not persisted_scope:
            # Store-scoped artifacts are system evidence (parser execution
            # receipts, operator manifests) and never belong to an actor.
            raise ResearchCatalogArtifactScopeError("research artifact is not visible to actor scope")
        if requested_scope and not actor_scope_matches(persisted_scope, requested_scope):
            raise ResearchCatalogArtifactScopeError("research artifact is not visible to actor scope")
        with locked_json_file(marker_path) as resolved:
            if not resolved.exists():
                raise ResearchCatalogStoreCorruptionError("research artifact commit marker is missing")
            marker = read_json_object_unlocked(resolved, default={}, strict=True)
        if (
            marker.get("status") != "committed"
            or marker.get("artifact_ref") != f"artifact://research/{artifact_type}/{digest}"
            or marker.get("content_checksum") != digest
        ):
            raise ResearchCatalogStoreCorruptionError("research artifact commit marker is invalid")
        result: dict[str, Any] = {
            "artifactRef": f"artifact://research/{artifact_type}/{digest}",
            "artifactType": artifact_type,
            "contentChecksum": f"sha256:{digest}",
            "metadata": dict(metadata),
            "committedAt": marker.get("created_at"),
        }
        if include_payload:
            payload = _operational_artifact_payload(
                artifact_type=artifact_type,
                payload=envelope.get("payload"),
                metadata=metadata,
                root=self.root,
                artifact_ref=f"artifact://research/{artifact_type}/{digest}",
                artifact_path=target,
                marker_path=marker_path,
            )
            encoded_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(encoded_payload) > max_chars:
                result["diagnostics"] = [{"code": "payload_truncated", "max_chars": max_chars}]
            else:
                result["payload"] = payload
        return result


def _portable_artifact_payload(
    *,
    artifact_type: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    stored = dict(payload)
    if artifact_type != "research-chunk-manifest":
        return stored
    raw_path = stored.get("manifest_path")
    if raw_path is None:
        return stored
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ResearchCatalogStoreError("research chunk manifest artifact path is invalid")
    expected = _expected_chunk_manifest_relative(payload=stored, metadata=metadata)
    _validate_canonical_filesystem_path(raw_path)
    candidate = Path(raw_path).expanduser()
    lexical_root = Path(os.path.abspath(root))
    if not candidate.is_absolute():
        cwd_candidate = Path(os.path.abspath(candidate))
        candidate = cwd_candidate if cwd_candidate.is_relative_to(lexical_root) else lexical_root / candidate
    try:
        portable = PurePosixPath(candidate.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix())
    except (OSError, ValueError) as exc:
        raise ResearchCatalogStoreError("research chunk manifest artifact path escapes the catalog owner") from exc
    if portable != expected:
        raise ResearchCatalogStoreError("research chunk manifest artifact path conflicts with its paper or actor scope")
    _require_owned_regular_file(root, candidate)
    stored["manifest_path"] = portable.as_posix()
    return stored


def _operational_artifact_payload(
    *,
    artifact_type: str,
    payload: Any,
    metadata: Mapping[str, Any],
    root: Path,
    artifact_ref: str,
    artifact_path: Path,
    marker_path: Path,
) -> Any:
    if artifact_type != "research-chunk-manifest":
        return payload
    if not isinstance(payload, Mapping):
        raise ResearchCatalogStoreCorruptionError("research chunk manifest artifact payload is invalid")
    projected = dict(payload)
    raw_path = projected.get("manifest_path")
    if raw_path is None:
        return projected
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ResearchCatalogStoreCorruptionError("research chunk manifest artifact path is invalid")
    try:
        if os.path.isabs(raw_path):
            _validate_canonical_filesystem_path(raw_path)
        expected = _expected_chunk_manifest_relative(payload=projected, metadata=metadata)
    except (ResearchCatalogStoreError, ValueError) as exc:
        raise ResearchCatalogStoreCorruptionError(str(exc)) from exc
    persisted = Path(raw_path).expanduser()
    if persisted.is_absolute():
        lexical = Path(os.path.abspath(persisted))
        lexical_root = Path(os.path.abspath(root))
        try:
            relative = PurePosixPath(lexical.relative_to(lexical_root).as_posix())
        except ValueError:
            relative = _restore_provenance_relative_path(
                root=root,
                artifact_ref=artifact_ref,
                artifact_path=artifact_path,
                marker_path=marker_path,
                legacy_path=raw_path,
                payload=projected,
                metadata=metadata,
            )
    else:
        try:
            relative = _safe_owner_relative_path(raw_path)
        except ResearchCatalogStoreError as exc:
            raise ResearchCatalogStoreCorruptionError(str(exc)) from exc
    if relative != expected:
        raise ResearchCatalogStoreCorruptionError("research chunk manifest artifact path conflicts with its paper or actor scope")
    candidate = root / Path(*relative.parts)
    try:
        _require_owned_regular_file(root, candidate)
    except ResearchCatalogStoreError as exc:
        raise ResearchCatalogStoreCorruptionError(str(exc)) from exc
    projected["manifest_path"] = str(candidate.resolve(strict=True))
    return projected


def _expected_chunk_manifest_relative(*, payload: Mapping[str, Any], metadata: Mapping[str, Any]) -> PurePosixPath:
    paper_id = _safe_segment(str(payload.get("paper_id") or ""), "paper_id")
    metadata_paper_id = _safe_segment(str(metadata.get("paper_id") or ""), "metadata paper_id")
    if metadata_paper_id != paper_id:
        raise ResearchCatalogStoreError("research chunk manifest paper identity conflicts with artifact metadata")
    payload_scope = _normalized_scope(payload.get("actor_scope") if isinstance(payload.get("actor_scope"), Mapping) else {})
    metadata_scope = _normalized_scope(metadata.get("actor_scope") if isinstance(metadata.get("actor_scope"), Mapping) else {})
    if payload_scope != metadata_scope:
        raise ResearchCatalogStoreError("research chunk manifest actor scope conflicts with artifact metadata")
    components = ["chunk-manifests"]
    if payload_scope:
        encoded = json.dumps(payload_scope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        components.append(hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24])
    components.extend((paper_id, "chunk_manifest.json"))
    return PurePosixPath(*components)


def _safe_owner_relative_path(value: str) -> PurePosixPath:
    if "\\" in value or "\x00" in value:
        raise ResearchCatalogStoreError("research artifact owner-relative path is invalid")
    components = value.split("/")
    if any(part in {"", ".", ".."} for part in components):
        raise ResearchCatalogStoreError("research artifact owner-relative path escapes the catalog owner")
    path = PurePosixPath(*components)
    if path.is_absolute():
        raise ResearchCatalogStoreError("research artifact owner-relative path escapes the catalog owner")
    return path


def _validate_canonical_filesystem_path(value: str) -> None:
    if "\x00" in value:
        raise ResearchCatalogStoreError("research chunk manifest artifact path is invalid")
    normalized = value.replace("\\", "/")
    if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/":
        normalized = normalized[3:]
    elif normalized.startswith("/"):
        normalized = normalized[1:]
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ResearchCatalogStoreError("research chunk manifest artifact path is not canonical")


def _require_owned_regular_file(root: Path, candidate: Path) -> None:
    lexical_root = Path(os.path.abspath(root))
    lexical_candidate = Path(os.path.abspath(candidate))
    try:
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise ResearchCatalogStoreError("research chunk manifest artifact target is outside the catalog owner") from exc
    root_attributes = getattr(lexical_root.lstat(), "st_file_attributes", 0)
    if lexical_root.is_symlink() or root_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ResearchCatalogStoreError("research catalog owner root is a reparse point")
    current = lexical_root
    for part in relative.parts:
        current = current / part
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError as exc:
            raise ResearchCatalogStoreError("research chunk manifest artifact target is missing") from exc
        if current.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ResearchCatalogStoreError("research chunk manifest artifact path contains a reparse point")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = lexical_candidate.resolve(strict=True)
    except OSError as exc:
        raise ResearchCatalogStoreError("research chunk manifest artifact target is missing") from exc
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise ResearchCatalogStoreError("research chunk manifest artifact target is outside the catalog owner")


def persist_restore_provenance(
    root: str | Path,
    *,
    backup_id: str,
    owner_id: str,
    owner_schema: str,
    trusted_backup_origins: Iterable[str | Path],
) -> dict[str, Any]:
    """Persist exact legacy artifact relocation authority inside one catalog owner."""

    if str(owner_id) != "research_paper_catalog" or not str(owner_schema).strip():
        raise ResearchCatalogStoreError("research catalog restore provenance owner identity is invalid")
    catalog_root = Path(os.path.abspath(Path(root).expanduser()))
    existing = _load_restore_provenance(catalog_root, required=False)
    existing_entries = existing.get("entries", {}) if existing else {}
    if not isinstance(existing_entries, Mapping):
        raise ResearchCatalogStoreCorruptionError("research catalog restore provenance entries are invalid")
    origins = tuple(
        sorted(
            {
                Path(os.path.abspath(Path(item).expanduser()))
                for item in trusted_backup_origins
            },
            key=lambda item: os.path.normcase(str(item)),
        )
    )
    if existing and existing.get("owner_schema") != str(owner_schema):
        raise ResearchCatalogStoreCorruptionError(
            "research catalog restore provenance owner schema is invalid"
        )
    entries: dict[str, dict[str, Any]] = {}
    artifact_root = catalog_root / "artifacts"
    for artifact_path in sorted(artifact_root.glob("*.json")) if artifact_root.is_dir() else ():
        if artifact_path.name.endswith(".commit.json"):
            continue
        envelope, artifact_ref, marker_path = _read_committed_artifact_envelope(
            catalog_root,
            artifact_path,
        )
        if envelope.get("artifact_type") != "research-chunk-manifest":
            continue
        payload = envelope.get("payload")
        metadata = envelope.get("metadata")
        if not isinstance(payload, Mapping) or not isinstance(metadata, Mapping):
            raise ResearchCatalogStoreCorruptionError("research chunk manifest artifact payload is invalid")
        raw_path = payload.get("manifest_path")
        if raw_path is None or not isinstance(raw_path, str) or not raw_path.strip():
            continue
        if os.path.isabs(raw_path):
            try:
                _validate_canonical_filesystem_path(raw_path)
            except ResearchCatalogStoreError as exc:
                raise ResearchCatalogStoreCorruptionError(str(exc)) from exc
        persisted = Path(raw_path).expanduser()
        if not persisted.is_absolute():
            continue
        lexical = Path(os.path.abspath(persisted))
        try:
            lexical.relative_to(catalog_root)
            continue
        except ValueError:
            pass
        prior = existing_entries.get(artifact_ref)
        if prior is not None:
            relative = _validate_restore_provenance_entry(
                prior,
                root=catalog_root,
                artifact_ref=artifact_ref,
                artifact_path=artifact_path,
                marker_path=marker_path,
                legacy_path=raw_path,
                payload=payload,
                metadata=metadata,
            )
            entries[artifact_ref] = dict(prior)
            if relative != _expected_chunk_manifest_relative(payload=payload, metadata=metadata):
                raise ResearchCatalogStoreCorruptionError("research catalog restore provenance target is invalid")
            continue
        expected = _expected_chunk_manifest_relative(payload=payload, metadata=metadata)
        matched_origin: Path | None = None
        relative: PurePosixPath | None = None
        for origin in origins:
            try:
                candidate_relative = PurePosixPath(lexical.relative_to(origin).as_posix())
            except ValueError:
                continue
            if candidate_relative == expected:
                matched_origin = origin
                relative = candidate_relative
                break
        if matched_origin is None or relative is None:
            raise ResearchCatalogStoreCorruptionError(
                "legacy research chunk manifest path has no verified backup owner association"
            )
        _require_owned_regular_file(catalog_root, catalog_root / Path(*relative.parts))
        digest = artifact_ref.rsplit("/", 1)[-1]
        entries[artifact_ref] = {
            "artifact_ref": artifact_ref,
            "artifact_type": "research-chunk-manifest",
            "content_checksum": f"sha256:{digest}",
            "artifact_file_sha256": _file_sha256(artifact_path),
            "marker_file_sha256": _file_sha256(marker_path),
            "legacy_manifest_path_sha256": _text_sha256(raw_path),
            "target_relative_path": relative.as_posix(),
            "paper_id": str(payload.get("paper_id") or ""),
            "actor_scope": _normalized_scope(metadata.get("actor_scope")),
            "established_from": {
                "backup_id": str(backup_id),
                "owner_id": str(owner_id),
                "owner_schema": str(owner_schema),
                "source_owner_identity": _text_sha256(
                    os.path.normcase(str(matched_origin))
                ),
            },
        }
    unexpected = sorted(set(existing_entries) - set(entries))
    if unexpected:
        raise ResearchCatalogStoreCorruptionError(
            "research catalog restore provenance references absent legacy artifacts"
        )
    if not entries and not existing:
        return {
            "schema": RESTORE_PROVENANCE_SCHEMA,
            "status": "not_required",
            "entry_count": 0,
        }
    payload = {
        "schema": RESTORE_PROVENANCE_SCHEMA,
        "owner_id": "research_paper_catalog",
        "owner_schema": str(owner_schema),
        "entries": {key: entries[key] for key in sorted(entries)},
    }
    checksum = _mapping_checksum(payload)
    document = {**payload, "content_checksum": checksum}
    marker = {
        "schema": RESTORE_PROVENANCE_SCHEMA,
        "record_type": "research_catalog_restore_provenance_commit",
        "status": "committed",
        "content_checksum": checksum,
    }
    provenance_path, marker_path = _restore_provenance_paths(catalog_root)
    if not existing or existing != document:
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        _reject_link_path(provenance_path.parent, catalog_root)
        write_json_object_unlocked(provenance_path, document)
        write_json_object_unlocked(marker_path, marker)
    return {
        "schema": RESTORE_PROVENANCE_SCHEMA,
        "path": str(provenance_path),
        "content_checksum": checksum,
        "entry_count": len(entries),
    }


def _restore_provenance_relative_path(
    *,
    root: Path,
    artifact_ref: str,
    artifact_path: Path,
    marker_path: Path,
    legacy_path: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> PurePosixPath:
    provenance = _load_restore_provenance(root, required=True)
    entries = provenance.get("entries")
    entry = entries.get(artifact_ref) if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping):
        raise ResearchCatalogStoreCorruptionError(
            "legacy research chunk manifest has no exact restore provenance"
        )
    return _validate_restore_provenance_entry(
        entry,
        root=root,
        artifact_ref=artifact_ref,
        artifact_path=artifact_path,
        marker_path=marker_path,
        legacy_path=legacy_path,
        payload=payload,
        metadata=metadata,
    )


def _validate_restore_provenance_entry(
    entry: Mapping[str, Any],
    *,
    root: Path,
    artifact_ref: str,
    artifact_path: Path,
    marker_path: Path,
    legacy_path: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> PurePosixPath:
    digest = artifact_ref.rsplit("/", 1)[-1]
    expected = _expected_chunk_manifest_relative(payload=payload, metadata=metadata)
    try:
        relative = _safe_owner_relative_path(str(entry.get("target_relative_path") or ""))
    except ResearchCatalogStoreError as exc:
        raise ResearchCatalogStoreCorruptionError(str(exc)) from exc
    established = entry.get("established_from")
    if (
        entry.get("artifact_ref") != artifact_ref
        or entry.get("artifact_type") != "research-chunk-manifest"
        or entry.get("content_checksum") != f"sha256:{digest}"
        or entry.get("artifact_file_sha256") != _file_sha256(artifact_path)
        or entry.get("marker_file_sha256") != _file_sha256(marker_path)
        or entry.get("legacy_manifest_path_sha256") != _text_sha256(legacy_path)
        or entry.get("paper_id") != str(payload.get("paper_id") or "")
        or _normalized_scope(entry.get("actor_scope"))
        != _normalized_scope(metadata.get("actor_scope"))
        or relative != expected
        or not isinstance(established, Mapping)
        or not str(established.get("backup_id") or "").strip()
        or established.get("owner_id") != "research_paper_catalog"
        or not str(established.get("owner_schema") or "").strip()
        or not _valid_sha256(str(established.get("source_owner_identity") or ""))
    ):
        raise ResearchCatalogStoreCorruptionError(
            "research catalog restore provenance does not match the committed artifact"
        )
    _require_owned_regular_file(root, root / Path(*relative.parts))
    return relative


def _load_restore_provenance(root: Path, *, required: bool) -> dict[str, Any]:
    provenance_path, marker_path = _restore_provenance_paths(root)
    if not provenance_path.exists() and not marker_path.exists():
        if required:
            raise ResearchCatalogStoreCorruptionError(
                "legacy research chunk manifest has no restore provenance"
            )
        return {}
    if not provenance_path.is_file() or not marker_path.is_file():
        raise ResearchCatalogStoreCorruptionError(
            "research catalog restore provenance is incomplete"
        )
    _require_owned_regular_file(root, provenance_path)
    _require_owned_regular_file(root, marker_path)
    try:
        document = json.loads(provenance_path.read_text(encoding="utf-8"))
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchCatalogStoreCorruptionError(
            "research catalog restore provenance is invalid"
        ) from exc
    if not isinstance(document, Mapping) or not isinstance(marker, Mapping):
        raise ResearchCatalogStoreCorruptionError(
            "research catalog restore provenance is invalid"
        )
    content = dict(document)
    checksum = content.pop("content_checksum", None)
    if (
        document.get("schema") != RESTORE_PROVENANCE_SCHEMA
        or document.get("owner_id") != "research_paper_catalog"
        or not str(document.get("owner_schema") or "").strip()
        or not isinstance(document.get("entries"), Mapping)
        or checksum != _mapping_checksum(content)
        or marker.get("schema") != RESTORE_PROVENANCE_SCHEMA
        or marker.get("record_type")
        != "research_catalog_restore_provenance_commit"
        or marker.get("status") != "committed"
        or marker.get("content_checksum") != checksum
    ):
        raise ResearchCatalogStoreCorruptionError(
            "research catalog restore provenance checksum is invalid"
        )
    return dict(document)


def _read_committed_artifact_envelope(
    root: Path,
    artifact_path: Path,
) -> tuple[dict[str, Any], str, Path]:
    _require_owned_regular_file(root, artifact_path)
    try:
        envelope = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchCatalogStoreCorruptionError(
            "research artifact envelope is invalid"
        ) from exc
    if not isinstance(envelope, dict):
        raise ResearchCatalogStoreCorruptionError(
            "research artifact envelope is invalid"
        )
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    artifact_type = str(envelope.get("artifact_type") or "")
    expected_name = f"{artifact_type}-{digest[:32]}.json"
    if artifact_path.name != expected_name:
        raise ResearchCatalogStoreCorruptionError(
            "research artifact checksum does not own its path"
        )
    artifact_ref = f"artifact://research/{artifact_type}/{digest}"
    marker_path = artifact_path.with_suffix(".commit.json")
    _require_owned_regular_file(root, marker_path)
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchCatalogStoreCorruptionError(
            "research artifact commit marker is invalid"
        ) from exc
    if (
        not isinstance(marker, Mapping)
        or marker.get("status") != "committed"
        or marker.get("artifact_ref") != artifact_ref
        or marker.get("artifact_type") != artifact_type
        or marker.get("content_checksum") != digest
    ):
        raise ResearchCatalogStoreCorruptionError(
            "research artifact commit marker is invalid"
        )
    return envelope, artifact_ref, marker_path


def _restore_provenance_paths(root: Path) -> tuple[Path, Path]:
    directory = root / _RESTORE_PROVENANCE_DIR
    return directory / "manifest.json", directory / "manifest.commit.json"


def _mapping_checksum(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_sha256(value: str) -> bool:
    raw = value.removeprefix("sha256:")
    return len(raw) == 64 and all(char in "0123456789abcdef" for char in raw.casefold())


def _reject_link_path(path: Path, root: Path) -> None:
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise ResearchCatalogStoreError(
            "research catalog restore provenance escapes the catalog owner"
        ) from exc
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            continue
        attributes = getattr(current.lstat(), "st_file_attributes", 0)
        if current.is_symlink() or attributes & getattr(
            stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        ):
            raise ResearchCatalogStoreError(
                "research catalog restore provenance contains a reparse point"
            )


class FilesystemResearchEventSink:
    """Append-only JSON event transcript for parse phase transitions."""

    def __init__(self, root: str | Path = ".newsroom/research_catalog") -> None:
        self.root = resolve_storage_path(root)

    def create_run_intent(
        self,
        run_id: str,
        *,
        request_fingerprint: str,
        actor_scope: Mapping[str, str],
        budget: Mapping[str, Any] | None = None,
    ) -> None:
        self.acquire_run_lease(
            run_id,
            request_fingerprint=request_fingerprint,
            actor_scope=actor_scope,
            budget=budget,
        )

    def acquire_run_lease(
        self,
        run_id: str,
        *,
        request_fingerprint: str,
        actor_scope: Mapping[str, str],
        budget: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically assign one run owner or return its durable state."""

        target = self._run_record_path(run_id, actor_scope, "intent")
        scope = _normalized_scope(actor_scope)
        payload = {
            "schema_version": 1,
            "record_type": "research_parse_run_intent",
            "record_id": f"{run_id}:intent",
            "run_id": run_id,
            "request_fingerprint": request_fingerprint,
            "actor_scope": scope,
            "budget": dict(budget or {}),
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
        }
        with locked_json_file(target) as resolved:
            if storage_io_path(resolved).exists():
                existing = read_json_object_unlocked(resolved, default={}, strict=True)
                if not isinstance(existing, Mapping):
                    raise ResearchCatalogStoreCorruptionError("research run intent is invalid")
                if (
                    existing.get("run_id") != run_id
                    or existing.get("request_fingerprint") != request_fingerprint
                    or _normalized_scope(existing.get("actor_scope")) != scope
                ):
                    raise ResearchCatalogStoreError("research run intent conflicts with existing run")
                final = self._read_final_record(run_id, scope)
                if final is not None:
                    return {"state": "completed", "final": final}
                recovery = self._read_recovery_record(run_id, scope)
                if recovery is not None and recovery.get("status") in {"quarantined", "orphaned"}:
                    return {"state": "recovery_required", "recovery": recovery}
                history = self.read_history(run_id, actor_scope=scope)
                return {
                    "state": "in_progress",
                    "last_committed_phase": history[-1].get("to_status") if history else "received",
                    "retry_after_seconds": 5,
                }
            write_json_object_unlocked(resolved, payload)
        return {"state": "owner", "run_id": run_id}

    def finalize(self, run_id: str, payload: Mapping[str, Any]) -> None:
        actor_scope = payload.get("actor_scope")
        scope = actor_scope if isinstance(actor_scope, Mapping) else {}
        normalized_scope = _normalized_scope(scope)
        intent_path = self._run_record_path(run_id, normalized_scope, "intent")
        with locked_json_file(intent_path) as resolved:
            if not storage_io_path(resolved).exists():
                raise ResearchCatalogStoreError("research final result has no run intent")
            intent = read_json_object_unlocked(resolved, default={}, strict=True)
        if not isinstance(intent, Mapping):
            raise ResearchCatalogStoreCorruptionError("research run intent is invalid")
        request_fingerprint = str(payload.get("request_fingerprint") or "").strip()
        if request_fingerprint and request_fingerprint != intent.get("request_fingerprint"):
            raise ResearchCatalogStoreError("research final result conflicts with run intent")
        target = self._run_record_path(run_id, normalized_scope, "final")
        record = {
            "schema_version": 1,
            "record_type": "research_parse_final_result",
            "record_id": f"{run_id}:final",
            "run_id": run_id,
            **dict(payload),
            "status": str(payload.get("status") or "failed"),
            "actor_scope": normalized_scope,
            "request_fingerprint": intent.get("request_fingerprint"),
            "committed_at": datetime.now(UTC).isoformat(),
        }
        with locked_json_file(target) as resolved:
            if storage_io_path(resolved).exists():
                existing = read_json_object_unlocked(resolved, default={}, strict=True)
                existing_comparable = {
                    key: value for key, value in existing.items()
                    if key != "committed_at"
                } if isinstance(existing, Mapping) else existing
                record_comparable = {
                    key: value for key, value in record.items()
                    if key != "committed_at"
                }
                if existing_comparable != record_comparable:
                    raise ResearchCatalogStoreError("research final result conflicts with existing run")
            else:
                write_json_object_unlocked(resolved, record)

    def load_final(
        self,
        run_id: str,
        *,
        actor_scope: Mapping[str, str],
    ) -> dict[str, Any] | None:
        return self._read_final_record(run_id, _normalized_scope(actor_scope))

    def append(self, run_id: str, event: dict[str, Any]) -> None:
        run = _safe_segment(run_id, "run_id")
        scope = event.get("actor_scope")
        scope_values = {
            str(key): str(value).strip()
            for key, value in (scope.items() if isinstance(scope, Mapping) else [])
            if str(key) in {"tenant_id", "user_id", "memory_namespace"} and str(value).strip()
        }
        # Keep public transcripts at the historical location. Scoped runs use
        # a digest directory so identical run IDs cannot share a transcript.
        if scope_values:
            digest = hashlib.sha256(actor_scope_ref(scope_values).encode("utf-8")).hexdigest()[:32]
            target = self.root / "events" / digest / f"{run}.jsonl"
        else:
            target = self.root / "events" / f"{run}.jsonl"
        with locked_json_file(target) as resolved:
            sequence = 1
            if storage_io_path(resolved).exists():
                with storage_io_path(resolved).open("r", encoding="utf-8") as existing:
                    for raw in existing:
                        if not raw.strip():
                            continue
                        try:
                            record = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise ResearchCatalogStoreCorruptionError(
                                "research event transcript contains invalid JSON"
                            ) from exc
                        if isinstance(record, Mapping) and isinstance(record.get("sequence"), int):
                            sequence = max(sequence, int(record["sequence"]) + 1)
            line = json.dumps(
                {**dict(event), "run_id": run_id, "sequence": sequence},
                ensure_ascii=False,
                sort_keys=True,
            )
            with storage_io_path(resolved).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def read_history(
        self,
        run_id: str,
        *,
        actor_scope: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Read and validate a run transcript, rejecting sequence gaps."""

        run = _safe_segment(run_id, "run_id")
        scope_values = _normalized_scope(actor_scope)
        if scope_values:
            digest = hashlib.sha256(actor_scope_ref(scope_values).encode("utf-8")).hexdigest()[:32]
            target = self.root / "events" / digest / f"{run}.jsonl"
        else:
            target = self.root / "events" / f"{run}.jsonl"
        if not storage_io_path(target).exists():
            return ()
        records: list[dict[str, Any]] = []
        expected = 1
        with locked_json_file(target) as resolved:
            with storage_io_path(resolved).open("r", encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ResearchCatalogStoreCorruptionError(
                            "research event transcript contains invalid JSON"
                        ) from exc
                    if not isinstance(record, dict) or record.get("run_id") != run:
                        raise ResearchCatalogStoreCorruptionError(
                            "research event transcript has invalid run identity"
                        )
                    if record.get("sequence") != expected:
                        raise ResearchCatalogStoreCorruptionError(
                            "research event transcript sequence gap"
                        )
                    records.append(record)
                    expected += 1
        return tuple(records)

    def scan_incomplete_runs(self) -> tuple[dict[str, Any], ...]:
        """Find durable run intents without a matching final result."""

        runs_io = storage_io_path(self.root / "runs")
        if not runs_io.exists():
            return ()
        results: list[dict[str, Any]] = []
        for intent_path in sorted(runs_io.glob("*/*.intent.json")):
            try:
                intent = read_json_object_unlocked(intent_path, default={}, strict=True)
            except Exception as exc:
                raise ResearchCatalogStoreCorruptionError(
                    "research run intent is invalid"
                ) from exc
            if not isinstance(intent, Mapping):
                raise ResearchCatalogStoreCorruptionError("research run intent is invalid")
            run_id = str(intent.get("run_id") or "").strip()
            if not run_id:
                raise ResearchCatalogStoreCorruptionError("research run intent has no run_id")
            final_path = intent_path.with_name(f"{run_id}.final.json")
            if final_path.exists():
                continue
            results.append(dict(intent))
        return tuple(results)

    def recover_incomplete_runs(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Replay terminal event evidence without rerunning a source or parser."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("recovery limit must be between 1 and 1000")
        results: list[dict[str, Any]] = []
        terminal_statuses = {
            "metadata_only",
            "parsed",
            "degraded",
            "catalog_partial",
            "catalog_ready",
            "failed",
        }
        for intent in self.scan_incomplete_runs()[:limit]:
            run_id = str(intent["run_id"])
            scope = _normalized_scope(intent.get("actor_scope"))
            recovery_run_id = "recovery-" + hashlib.sha256(
                f"{run_id}|{datetime.now(UTC).isoformat()}".encode("utf-8")
            ).hexdigest()[:20]
            try:
                history = self.read_history(run_id, actor_scope=scope)
            except ResearchCatalogStoreCorruptionError as exc:
                results.append(
                    self._write_recovery_record(
                        run_id,
                        scope,
                        status="quarantined",
                        recovery_run_id=recovery_run_id,
                        reason_code="event_transcript_corrupt",
                        details={"error_type": type(exc).__name__},
                    )
                )
                continue
            terminal = next(
                (
                    event
                    for event in reversed(history)
                    if event.get("terminal") is True
                    and str(event.get("to_status") or event.get("status") or "") in terminal_statuses
                ),
                None,
            )
            paper_id = str((terminal or {}).get("paper_id") or "").strip()
            if terminal is None or not paper_id:
                results.append(
                    self._write_recovery_record(
                        run_id,
                        scope,
                        status="quarantined",
                        recovery_run_id=recovery_run_id,
                        reason_code="terminal_event_missing",
                        details={"history_events": len(history)},
                    )
                )
                continue
            status = str(terminal.get("to_status") or terminal.get("status"))
            snapshot_id = str(terminal.get("source_snapshot_id") or "").strip()
            final_payload = {
                "status": status,
                "paper_id": paper_id,
                "catalog_status": terminal.get("catalog_status"),
                "artifact_refs": list(terminal.get("artifact_refs") or []),
                "source_snapshot_refs": list(terminal.get("source_snapshot_refs") or ([snapshot_id] if snapshot_id else [])),
                "diagnostics": list(terminal.get("diagnostics") or []),
                "actor_scope": scope,
                "request_fingerprint": intent.get("request_fingerprint"),
                "recovered_from_event_id": terminal.get("event_id"),
                "recovery_run_id": recovery_run_id,
            }
            try:
                self.finalize(run_id, final_payload)
            except Exception as exc:
                results.append(
                    self._write_recovery_record(
                        run_id,
                        scope,
                        status="quarantined",
                        recovery_run_id=recovery_run_id,
                        reason_code="final_replay_failed",
                        details={"error_type": type(exc).__name__},
                    )
                )
                continue
            self.append(
                run_id,
                {
                    "event_id": f"{recovery_run_id}:final-replayed",
                    "event_type": "research_parse_recovery",
                    "status": "recovered",
                    "actor_scope": scope,
                    "recovery_run_id": recovery_run_id,
                    "recovered_from_event_id": terminal.get("event_id"),
                    "reason_code": "final_result_replayed",
                },
            )
            results.append(
                {
                    "run_id": run_id,
                    "status": "recovered",
                    "recovery_run_id": recovery_run_id,
                    "paper_id": paper_id,
                }
            )
        return tuple(results)

    def _read_final_record(
        self,
        run_id: str,
        actor_scope: Mapping[str, str],
    ) -> dict[str, Any] | None:
        target = self._run_record_path(run_id, actor_scope, "final")
        with locked_json_file(target) as resolved:
            if not storage_io_path(resolved).exists():
                return None
            record = read_json_object_unlocked(resolved, default={}, strict=True)
        if not isinstance(record, Mapping):
            raise ResearchCatalogStoreCorruptionError("research final result is invalid")
        if record.get("run_id") != run_id or _normalized_scope(record.get("actor_scope")) != _normalized_scope(actor_scope):
            raise ResearchCatalogStoreCorruptionError("research final result has invalid identity")
        return dict(record)

    def _read_recovery_record(
        self,
        run_id: str,
        actor_scope: Mapping[str, str],
    ) -> dict[str, Any] | None:
        target = self._run_record_path(run_id, actor_scope, "recovery")
        with locked_json_file(target) as resolved:
            if not storage_io_path(resolved).exists():
                return None
            record = read_json_object_unlocked(resolved, default={}, strict=True)
        if not isinstance(record, Mapping):
            raise ResearchCatalogStoreCorruptionError("research run recovery record is invalid")
        if record.get("run_id") != run_id or _normalized_scope(record.get("actor_scope")) != _normalized_scope(actor_scope):
            raise ResearchCatalogStoreCorruptionError("research run recovery record has invalid identity")
        return dict(record)

    def _write_recovery_record(
        self,
        run_id: str,
        actor_scope: Mapping[str, str],
        *,
        status: str,
        recovery_run_id: str,
        reason_code: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        scope = _normalized_scope(actor_scope)
        record = {
            "schema_version": 1,
            "record_type": "research_parse_recovery",
            "record_id": f"{run_id}:recovery",
            "run_id": run_id,
            "status": status,
            "recovery_run_id": recovery_run_id,
            "reason_code": reason_code,
            "details": dict(details),
            "actor_scope": scope,
            "created_at": datetime.now(UTC).isoformat(),
        }
        target = self._run_record_path(run_id, scope, "recovery")
        with locked_json_file(target) as resolved:
            if storage_io_path(resolved).exists():
                existing = read_json_object_unlocked(resolved, default={}, strict=True)
                if not isinstance(existing, Mapping):
                    raise ResearchCatalogStoreCorruptionError("research run recovery record is invalid")
                return dict(existing)
            write_json_object_unlocked(resolved, record)
        self.append(
            run_id,
            {
                "event_id": f"{recovery_run_id}:quarantined",
                "event_type": "research_parse_recovery",
                "status": status,
                "actor_scope": scope,
                "recovery_run_id": recovery_run_id,
                "reason_code": reason_code,
            },
        )
        return record

    def _run_record_path(
        self,
        run_id: str,
        actor_scope: Mapping[str, str],
        record_kind: str,
    ) -> Path:
        run = _safe_segment(run_id, "run_id")
        scope = {
            str(key): str(value).strip()
            for key, value in actor_scope.items()
            if str(key) in {"tenant_id", "user_id", "memory_namespace"} and str(value).strip()
        }
        digest = hashlib.sha256(actor_scope_ref(scope).encode("utf-8")).hexdigest()[:32]
        return self.root / "runs" / digest / f"{run}.{record_kind}.json"

_STATE_COLLECTIONS = (
    "catalog",
    "identities",
    "relations",
    "snapshots",
    "papers",
    "documents",
    "evidence",
    "scores",
    "sota_claims",
    "code_profiles",
)


def _empty_state(scope_ref: str) -> dict[str, Any]:
    return {
        "schema_version": CATALOG_STORE_SCHEMA_VERSION,
        "scope_ref": scope_ref,
        **{key: {} for key in _STATE_COLLECTIONS},
    }


def _checksum(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"value does not support model_dump: {type(value).__name__}")


def _put_identity(
    state: dict[str, Any],
    key: str,
    value: ResearchPaperIdentity,
) -> None:
    """Persist append-only identity provenance inside the file transaction.

    Application services own canonical identity resolution. The store protects
    observation provenance from stale concurrent writers by merging against the
    latest durable identity while the catalog file lock is held.
    """

    identities = state["identities"]
    existing = _model(ResearchPaperIdentity, identities.get(key))
    merged_fields: dict[str, Any] = {}
    if existing is not None:
        if existing.paper_id != value.paper_id:
            raise ResearchCatalogStoreError("paper identity key conflicts with an existing paper_id")
        # Concatenate observations only; domain validators own normalization and
        # deduplication, including synchronization of snapshot ids and refs.
        merged_fields = {
            "source_snapshot_ids": [*existing.source_snapshot_ids, *value.source_snapshot_ids],
            "source_snapshot_refs": [*existing.source_snapshot_refs, *value.source_snapshot_refs],
            "field_provenance": {
                field_name: [
                    *existing.field_provenance.get(field_name, []),
                    *value.field_provenance.get(field_name, []),
                ]
                for field_name in set(existing.field_provenance) | set(value.field_provenance)
            },
        }
    identities[key] = _dump(
        ResearchPaperIdentity.model_validate(value.model_dump(mode="python") | merged_fields)
    )


def _put_immutable_snapshot(state: dict[str, Any], key: str, snapshot: ResearchSourceSnapshot) -> None:
    # model_copy() may leave flat scope metadata unnormalized until a read.
    # Compare canonical model representations, retaining the original immutable
    # observation when only that deterministic round-trip normalization differs.
    value = _dump(ResearchSourceSnapshot.model_validate(_dump(snapshot)))
    existing = state["snapshots"].get(key)
    if existing is not None:
        if _dump(_model(ResearchSourceSnapshot, existing)) != value:
            raise ResearchCatalogStoreError(
                "source snapshot is immutable and conflicts with an existing snapshot_id"
            )
        return
    state["snapshots"][key] = value


def _model(model_type, raw: Any):
    if raw is None:
        return None
    try:
        return model_type.model_validate(raw)
    except Exception as exc:
        raise ResearchCatalogStoreCorruptionError(f"stored {model_type.__name__} is invalid") from exc


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _scoped_key(identifier: str, scope: Mapping[str, Any] | None) -> str:
    raw_scope: Mapping[str, Any] = scope or {}
    nested_scope = raw_scope.get("actor_scope") if isinstance(raw_scope, Mapping) else None
    if isinstance(nested_scope, Mapping):
        raw_scope = {**dict(raw_scope), **dict(nested_scope)}
    values = {
        str(key): str(value).strip()
        for key, value in raw_scope.items()
        if str(key) in {"tenant_id", "user_id", "memory_namespace"} and str(value).strip()
    }
    return str(identifier) if not values else f"{actor_scope_ref(values)}::{identifier}"


def _snapshot_scope(snapshot: ResearchSourceSnapshot) -> Mapping[str, Any]:
    scope: dict[str, Any] = {}
    typed = getattr(snapshot, "actor_scope", None)
    if isinstance(typed, Mapping):
        scope.update(typed)
    for source in (snapshot.metadata, snapshot.lineage.metadata):
        if not isinstance(source, Mapping):
            continue
        nested = source.get("actor_scope")
        if isinstance(nested, Mapping):
            scope.update(nested)
        for key in ("tenant_id", "user_id", "memory_namespace"):
            if source.get(key) is not None and str(source.get(key)).strip():
                scope[key] = str(source[key]).strip()
    return scope


def _value_scope(value: Any) -> Mapping[str, Any]:
    typed = getattr(value, "actor_scope", None)
    if isinstance(typed, Mapping) and typed:
        return typed
    metadata = getattr(value, "metadata", None)
    if isinstance(metadata, Mapping):
        nested = metadata.get("actor_scope")
        if isinstance(nested, Mapping):
            return nested
        dimensions = {
            key: metadata.get(key)
            for key in ("tenant_id", "user_id", "memory_namespace")
            if metadata.get(key) is not None
        }
        if dimensions:
            return dimensions
    lineage = getattr(value, "lineage", None)
    lineage_metadata = getattr(lineage, "metadata", None)
    return lineage_metadata if isinstance(lineage_metadata, Mapping) else {}


def _safe_segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or any(char in text for char in "\\/:\x00"):
        raise ValueError(f"{label} contains an unsafe path segment")
    return text


def _normalized_scope(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("actor_scope")
    source = {**dict(value), **(dict(nested) if isinstance(nested, Mapping) else {})}
    return {
        key: str(source[key]).strip()
        for key in ("tenant_id", "user_id", "memory_namespace")
        if source.get(key) is not None and str(source[key]).strip()
    }


__all__ = [
    "CATALOG_STORE_SCHEMA_VERSION",
    "FilesystemResearchCatalogStore",
    "FilesystemResearchEventSink",
    "ResearchCatalogArtifactNotFoundError",
    "ResearchCatalogArtifactScopeError",
    "ResearchCatalogStoreCorruptionError",
    "ResearchCatalogStoreError",
    "RESTORE_PROVENANCE_SCHEMA",
    "persist_restore_provenance",
]
