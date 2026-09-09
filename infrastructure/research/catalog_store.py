from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
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
from infrastructure.storage.json_file_store import (
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)


CATALOG_STORE_SCHEMA_VERSION = 2
_LEGACY_CATALOG_STORE_SCHEMA_VERSIONS = frozenset({1})


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
            self._mutate(lambda state: state["identities"].__setitem__(_scoped_key(value.paper_id, value.metadata.get("actor_scope")), _dump(value)))
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
        envelope = {
            "artifact_type": artifact_type,
            "metadata": artifact_metadata,
            "payload": dict(payload),
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
            payload = envelope.get("payload")
            encoded_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(encoded_payload) > max_chars:
                result["diagnostics"] = [{"code": "payload_truncated", "max_chars": max_chars}]
            else:
                result["payload"] = payload
        return result


class FilesystemResearchEventSink:
    """Append-only JSON event transcript for parse phase transitions."""

    def __init__(self, root: str | Path = ".newsroom/research_catalog") -> None:
        self.root = Path(root).expanduser()

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
            if resolved.exists():
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
            if not resolved.exists():
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
            if resolved.exists():
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
            if resolved.exists():
                with resolved.open("r", encoding="utf-8") as existing:
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
            with resolved.open("a", encoding="utf-8") as handle:
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
        if not target.exists():
            return ()
        records: list[dict[str, Any]] = []
        expected = 1
        with locked_json_file(target) as resolved:
            with resolved.open("r", encoding="utf-8") as handle:
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

        if not (self.root / "runs").exists():
            return ()
        results: list[dict[str, Any]] = []
        for intent_path in sorted((self.root / "runs").glob("*/*.intent.json")):
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
            if not resolved.exists():
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
            if not resolved.exists():
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
            if resolved.exists():
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
]
