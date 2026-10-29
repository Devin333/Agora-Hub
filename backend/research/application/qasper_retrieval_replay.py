"""Deterministic retrieval-only replay for a frozen QASPER answer sample.

The replay deliberately runs the production ``PaperRAGSession`` retrieval and
context assembly path with generation disabled.  It consumes only immutable local
artifacts and a read-only query-vector snapshot; no model, judge, download, index
write, memory write, or publication side effect is available on this path.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.paper_rag_session import PaperRAGSession
from backend.research.application.qasper_assets import QasperAssetService
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.ports.field_embedding_index import FieldEmbeddingSearchPort
from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.ports.qasper_query_embedding import QueryVectorSnapshot, QueryVectorStorePort
from backend.research.ports.reranker import RerankerPort
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.qasper_answer_sampling import SampledQuestion, sample_questions
from backend.research.rag.evaluation.qasper_mapping import FrozenQasperCase, freeze_qasper_dataset
from backend.research.rag.evaluation.qasper_retrieval import (
    CachedFixtureChunkStore,
    OfflineEmbeddingCache,
    QasperOfflineCacheError,
)
from backend.research.rag.evaluation.qasper_scoring import evidence_group_coverage
from backend.research.rag.retrieval.policies import RetrievalPolicy
from framework.harness.rag.models import RAGBudget, RAGSessionSpec

PROTOCOL = "qasper_retrieval_only_replay_v1"
FIXTURE_BOUNDARY = (
    "Read-only in-memory cached-vector fixture replay; this is not a production "
    "Qdrant quality or latency measurement."
)


class QasperReplayPreflightError(RuntimeError):
    """A replay identity or required offline vector is unavailable."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = str(code)
        self.reason = str(reason)


@dataclass(frozen=True)
class QasperRetrievalReplayProfile:
    name: str
    policy: RetrievalPolicy
    field_index_enabled: bool = False
    reranker_enabled: bool = False
    field_reranker_enabled: bool = False
    max_context_items: int = 8
    max_context_tokens: int = 2048

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("replay profile name is required")
        if type(self.max_context_items) is not int or self.max_context_items < 1:
            raise ValueError("max_context_items must be a positive integer")
        if type(self.max_context_tokens) is not int or self.max_context_tokens < 1:
            raise ValueError("max_context_tokens must be a positive integer")
        for field_name in ("field_index_enabled", "reranker_enabled", "field_reranker_enabled"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")

    def identity(self, components: Mapping[str, str | None]) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "policy": asdict(self.policy),
            "components": dict(components),
            "context_budget": {
                "max_context_items": self.max_context_items,
                "max_context_tokens": self.max_context_tokens,
            },
            "execution_profile": "fixture",
            "fixture_boundary": FIXTURE_BOUNDARY,
        }
        return {**payload, "fingerprint": content_digest(payload)}


@dataclass(frozen=True)
class QasperRetrievalReplayConfig:
    source_run_directory: str
    output_directory: str
    run_id: str = "qasper-retrieval-replay"

    def __post_init__(self) -> None:
        if not self.source_run_directory.strip():
            raise ValueError("source_run_directory is required")
        if not self.output_directory.strip():
            raise ValueError("output_directory is required")
        if not self.run_id.strip():
            raise ValueError("run_id is required")


FieldIndexFactory = Callable[
    [Sequence[Any], OfflineEmbeddingCache, Callable[[str], np.ndarray]],
    FieldEmbeddingSearchPort,
]


class ReadOnlyQueryVectorResolver:
    """Resolve cached query vectors and record every miss without live fallback."""

    def __init__(
        self,
        cache: OfflineEmbeddingCache,
        snapshot: QueryVectorSnapshot | None,
        *,
        model_id: str,
    ) -> None:
        self._cache = cache
        self._dimension = int(cache.matrix.shape[1])
        self._vectors: dict[str, np.ndarray] = {}
        self.missing_keys: set[str] = set()
        self._missing_queries: dict[str, dict[str, Any]] = {}
        self._profile: str | None = None
        self._case_id: str | None = None
        self._initial_queries: frozenset[str] = frozenset()
        self._channel: str | None = None
        self.base_hits = 0
        self.supplementary_hits = 0
        if snapshot is None:
            return
        if snapshot.base_cache_sha256 != cache.cache_id:
            raise QasperReplayPreflightError(
                "query_vector_identity_mismatch",
                "supplementary query vectors were built for a different base cache",
            )
        if snapshot.model_id != model_id or int(snapshot.dimension) != self._dimension:
            raise QasperReplayPreflightError(
                "query_vector_identity_mismatch",
                "supplementary query vectors use a different model or dimension",
            )
        for key, values in snapshot.vectors.items():
            vector = np.asarray(values, dtype=np.float32)
            if vector.shape != (self._dimension,) or not np.isfinite(vector).all():
                raise QasperReplayPreflightError(
                    "query_vector_invalid", f"supplementary query vector {key} is malformed",
                )
            vector.setflags(write=False)
            self._vectors[str(key)] = vector

    def __call__(self, query_text: str) -> np.ndarray:
        key = OfflineEmbeddingCache.key(query_text)
        if key in self._cache.key_to_row:
            self.base_hits += 1
            return self._cache.vector_for(query_text)
        vector = self._vectors.get(key)
        if vector is not None:
            self.supplementary_hits += 1
            return vector
        self._record_missing(query_text, key=key)
        raise QasperOfflineCacheError(
            f"required query vector is absent for text digest {key}; retrieval replay never calls an embedder"
        )

    def require_queries(self, queries: Sequence[tuple[str, str, str]]) -> None:
        """Require a vector for every ``(query, case_id, profile)`` before replay."""
        for query, case_id, profile in queries:
            key = OfflineEmbeddingCache.key(query)
            if key in self._cache.key_to_row or key in self._vectors:
                continue
            self._record_missing(
                query,
                key=key,
                affected_profiles=(profile,),
                affected_case_ids=(case_id,),
                channel="preflight_initial_query",
                query_kind="initial_gap_focused",
            )
        self.raise_if_missing(code="query_vectors_missing")

    def set_case_context(
        self, *, profile: str, case_id: str, initial_queries: Sequence[str],
    ) -> None:
        self._profile = profile
        self._case_id = case_id
        self._initial_queries = frozenset(initial_queries)

    def clear_case_context(self) -> None:
        self._profile = None
        self._case_id = None
        self._initial_queries = frozenset()
        self._channel = None

    def set_channel(self, channel: str | None) -> None:
        self._channel = str(channel) if channel else None

    def missing_query_vectors(self) -> list[dict[str, Any]]:
        return [
            {
                "query_digest": key,
                "query_text": row["query_text"],
                "query_kinds": sorted(row["query_kinds"]),
                "retrieval_channels": sorted(row["retrieval_channels"]),
                "affected_profiles": sorted(row["affected_profiles"]),
                "affected_case_ids": sorted(row["affected_case_ids"]),
                "occurrences": row["occurrences"],
            }
            for key, row in sorted(self._missing_queries.items())
        ]

    def _record_missing(
        self,
        query_text: str,
        *,
        key: str,
        affected_profiles: Sequence[str] = (),
        affected_case_ids: Sequence[str] = (),
        channel: str | None = None,
        query_kind: str | None = None,
    ) -> None:
        self.missing_keys.add(key)
        row = self._missing_queries.setdefault(key, {
            "query_text": str(query_text),
            "query_kinds": set(),
            "retrieval_channels": set(),
            "affected_profiles": set(),
            "affected_case_ids": set(),
            "occurrences": 0,
        })
        row["occurrences"] += 1
        row["affected_profiles"].update(str(value) for value in affected_profiles if str(value))
        row["affected_case_ids"].update(str(value) for value in affected_case_ids if str(value))
        if self._profile:
            row["affected_profiles"].add(self._profile)
        if self._case_id:
            row["affected_case_ids"].add(self._case_id)
        actual_channel = channel or self._channel
        if actual_channel:
            row["retrieval_channels"].add(actual_channel)
        actual_kind = query_kind or (
            "initial_gap_focused"
            if query_text in self._initial_queries
            else "controlled_replan_or_pipeline_rewrite"
        )
        row["query_kinds"].add(actual_kind)

    def raise_if_missing(self, *, code: str) -> None:
        if self.missing_keys:
            raise QasperReplayPreflightError(
                code,
                f"supplementary query cache is missing {len(self.missing_keys)} required query vector(s)",
            )

    def identity(self, snapshot: QueryVectorSnapshot | None) -> dict[str, Any]:
        vector_digests = []
        for key, values in sorted((snapshot.vectors if snapshot else {}).items()):
            vector = np.asarray(values, dtype=np.float32)
            vector_digests.append((key, hashlib.sha256(vector.tobytes()).hexdigest()))
        return {
            "base_cache_sha256": snapshot.base_cache_sha256 if snapshot else None,
            "model_id": snapshot.model_id if snapshot else None,
            "dimension": int(snapshot.dimension) if snapshot else None,
            "vector_count": len(snapshot.vectors) if snapshot else 0,
            "content_fingerprint": content_digest(vector_digests),
            "mode": "read_only_no_live_fallback",
        }

    def stats(self) -> dict[str, int]:
        return {
            "base_hits": self.base_hits,
            "supplementary_hits": self.supplementary_hits,
            "live_calls": 0,
            "missing_keys": len(self.missing_keys),
        }


class QasperRetrievalReplayService:
    def __init__(
        self,
        assets: QasperAssetService,
        artifacts: QasperArtifactStore,
        *,
        query_vector_store: QueryVectorStorePort,
        profiles: Sequence[QasperRetrievalReplayProfile],
        config: QasperRetrievalReplayConfig,
        field_index_factory: FieldIndexFactory | None = None,
        rerankers: Mapping[str, RerankerPort] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._assets = assets
        self._artifacts = artifacts
        self._query_vector_store = query_vector_store
        self._profiles = tuple(profiles)
        self._config = config
        self._field_index_factory = field_index_factory
        self._rerankers = dict(rerankers or {})
        self._clock = clock
        if not self._profiles or len({profile.name for profile in self._profiles}) != len(self._profiles):
            raise ValueError("replay profiles must be non-empty and uniquely named")
        budgets = {
            (profile.max_context_items, profile.max_context_tokens)
            for profile in self._profiles
        }
        if len(budgets) != 1:
            raise ValueError("all replay profiles must use the same final context budget")
        for profile in self._profiles:
            if profile.field_index_enabled and field_index_factory is None:
                raise ValueError(f"profile {profile.name!r} enables field retrieval without a field index factory")
            if (profile.reranker_enabled or profile.field_reranker_enabled) and profile.name not in self._rerankers:
                raise ValueError(f"profile {profile.name!r} enables reranking without a reranker")

    def run(self) -> dict[str, Any]:
        started = self._clock()
        manifest: dict[str, Any] = {
            "protocol": PROTOCOL,
            "status": "preflight",
            "execution_profile": "fixture",
            "fixture_boundary": FIXTURE_BOUNDARY,
            "source_run_directory": self._config.source_run_directory,
            "output_directory": self._config.output_directory,
            "profiles": [],
            "limitations": {
                "generation": "disabled; no answer model or judge is constructed",
                "comparison": (
                    "This replay measures pre-generation retrieval/context recall. Harness replans and "
                    "context assembly may differ from the historical answered run, so replay coverage is "
                    "not an answer-quality score and is not directly comparable to historical answered-run "
                    "context coverage."
                ),
            },
        }
        prepared: _PreparedReplay | None = None
        try:
            prepared = self._preflight(manifest)
            rows, summaries = self._execute(prepared, manifest)
            if prepared.resolver.missing_keys:
                manifest["missing_query_vectors"] = prepared.resolver.missing_query_vectors()
                raise QasperReplayPreflightError(
                    "query_vectors_missing_during_replay",
                    f"replay encountered {len(prepared.resolver.missing_keys)} uncached query vector(s)",
                )
            manifest["status"] = "completed"
            report = {
                "protocol": PROTOCOL,
                "status": "completed",
                "identity": manifest["identity"],
                "source": manifest["source"],
                "profiles": summaries,
                "case_count": len(prepared.questions),
                "query_vectors": prepared.resolver.stats(),
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
                "limitations": manifest["limitations"],
            }
            self._publish(manifest, report, rows)
            return report
        except QasperReplayPreflightError as exc:
            manifest["status"] = "blocked"
            if prepared is not None and "missing_query_vectors" not in manifest:
                manifest["missing_query_vectors"] = prepared.resolver.missing_query_vectors()
            manifest.setdefault("missing_query_vectors", [])
            report = {
                "protocol": PROTOCOL,
                "status": "blocked",
                "blocked": {"code": exc.code, "reason": exc.reason},
                "identity": manifest.get("identity"),
                "source": manifest.get("source"),
                "profiles": [],
                "missing_query_vectors": manifest["missing_query_vectors"],
                "case_count": 0,
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
                "limitations": manifest["limitations"],
            }
            self._publish(manifest, report, ())
            return report

    def _preflight(self, manifest: dict[str, Any]) -> "_PreparedReplay":
        assets = self._assets.read_assets()
        frozen = freeze_qasper_dataset(assets.raw_dataset, assets.documents, assets.chunks)
        frozen.verify_digest()
        cache = self._assets.embedding_cache()
        cache_identity = self._assets.cache_identity()
        snapshot = self._query_vector_store.load()
        resolver = ReadOnlyQueryVectorResolver(cache, snapshot, model_id=cache_identity.model_id)

        sample_bytes = self._artifacts.read(self._config.source_run_directory, "sample.json")
        report_bytes = self._artifacts.read(self._config.source_run_directory, "report.json")
        answers_bytes = self._artifacts.read(self._config.source_run_directory, "answers.jsonl")
        source_sample = _json_object(sample_bytes, "sample")
        source_report = _json_object(report_bytes, "report")
        answer_rows = _json_lines(answers_bytes, "answers")
        questions = _verify_fixed_sample(assets.raw_dataset, source_sample, answer_rows)
        source_identity = dict(source_report.get("identity") or {})
        _require_legacy_identity(source_identity, "dataset_sha256", assets.dataset_sha256)
        _require_legacy_identity(source_identity, "mapping_digest", frozen.digest)
        _require_legacy_identity(source_identity, "embedding_cache_sha256", cache_identity.sha256)
        _require_legacy_identity(source_identity, "embedding_model", cache_identity.model_id)
        _require_legacy_identity(source_identity, "embedding_dimension", cache_identity.dimension)
        _match_optional_identity(source_identity, "corpus_sha256", frozen.corpus_sha256)
        _match_optional_identity(source_identity, "chunk_index_sha256", frozen.index_sha256)
        _validate_optional_source_profile_fingerprint(source_identity)
        if source_report.get("evaluated_cases") not in (None, len(questions)):
            raise QasperReplayPreflightError(
                "source_case_count_mismatch", "source report evaluated_cases does not match the frozen sample",
            )

        frozen_by_id = {case.case_id: case for case in frozen.cases}
        missing_mapping = [question.case_id for question in questions if question.case_id not in frozen_by_id]
        if missing_mapping:
            raise QasperReplayPreflightError(
                "source_mapping_mismatch", f"{len(missing_mapping)} source case(s) are absent from frozen mapping",
            )

        run_chunks = tuple(
            chunk.model_copy(update={"metadata": {**chunk.metadata, "run_id": self._config.run_id}})
            for chunk in assets.chunks
        )
        # Construct every selected field index before the first profile/case.  This
        # proves cache completeness and prevents a selected field profile degrading.
        field_indexes: dict[str, FieldEmbeddingSearchPort] = {}
        for profile in self._profiles:
            if profile.field_index_enabled:
                assert self._field_index_factory is not None
                try:
                    field_indexes[profile.name] = self._field_index_factory(run_chunks, cache, resolver)
                except QasperOfflineCacheError as exc:
                    raise QasperReplayPreflightError("field_vectors_missing", str(exc)) from exc

        profile_runs = {
            profile.name: self._profile_run(
                profile,
                chunks=run_chunks,
                cache=cache,
                resolver=resolver,
                field_index=field_indexes.get(profile.name),
                questions=questions,
            )
            for profile in self._profiles
        }
        # Each session's first-round plan is fixed before retrieval, so its queries
        # are required for every case up front. Controlled replan queries depend on
        # the evidence retrieved; the read-only resolver records any that are absent
        # during execution and blocks publication of all coverage.
        try:
            resolver.require_queries([
                (query, case_id, profile_name)
                for profile_name, run in profile_runs.items()
                for case_id, queries in run.initial_queries.items()
                for query in queries
            ])
        except QasperReplayPreflightError:
            manifest["missing_query_vectors"] = resolver.missing_query_vectors()
            raise

        query_identity = resolver.identity(snapshot)
        identity = {
            "dataset_sha256": assets.dataset_sha256,
            "mapping_digest": frozen.digest,
            "corpus_sha256": frozen.corpus_sha256,
            "chunk_index_sha256": frozen.index_sha256,
            "embedding_cache_sha256": cache_identity.sha256,
            "embedding_model": cache_identity.model_id,
            "embedding_dimension": cache_identity.dimension,
            "query_vectors": query_identity,
            "sample_case_ids_digest": content_digest([question.case_id for question in questions]),
            "code": assets.code_identity.model_dump(mode="json"),
            "fixture_boundary": FIXTURE_BOUNDARY,
        }
        source = {
            "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(),
            "answers_sha256": hashlib.sha256(answers_bytes).hexdigest(),
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "legacy_identity": source_identity,
            "legacy_missing_identity_fields": sorted(
                {"corpus_sha256", "retrieval_profile_fingerprint", "retrieval_policy_settings"}
                - source_identity.keys()
            ),
            "case_count": len(questions),
        }
        manifest["identity"] = identity
        manifest["source"] = source
        manifest["profiles"] = [
            self._profile_identity(profile, field_index=field_indexes.get(profile.name))
            for profile in self._profiles
        ]
        manifest["manifest_fingerprint"] = content_digest({
            "identity": identity,
            "source": source,
            "profiles": manifest["profiles"],
        })
        return _PreparedReplay(
            assets=assets,
            cache=cache,
            resolver=resolver,
            questions=questions,
            frozen_by_id=frozen_by_id,
            profile_runs=profile_runs,
        )

    def _execute(
        self, prepared: "_PreparedReplay", manifest: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        all_rows: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        expected_ids = tuple(question.case_id for question in prepared.questions)
        identities = {
            str(item["name"]): item
            for item in manifest["profiles"]
        }
        for profile in self._profiles:
            run = prepared.profile_runs[profile.name]
            rows = []
            for index, question in enumerate(prepared.questions):
                prepared.resolver.set_case_context(
                    profile=profile.name,
                    case_id=question.case_id,
                    initial_queries=run.initial_queries[question.case_id],
                )
                try:
                    rows.append(self._run_case(
                        profile,
                        question,
                        prepared.frozen_by_id[question.case_id],
                        run=run,
                        index=index,
                    ))
                finally:
                    prepared.resolver.clear_case_context()
            if tuple(row["case_id"] for row in rows) != expected_ids:
                raise RuntimeError(f"profile {profile.name!r} did not preserve fixed case identities")
            all_rows.extend(rows)
            summaries.append(_profile_summary(profile, rows, identities[profile.name]))
        return all_rows, summaries

    def _run_case(
        self,
        profile: QasperRetrievalReplayProfile,
        question: SampledQuestion,
        frozen_case: FrozenQasperCase,
        *,
        run: "_ProfileRun",
        index: int,
    ) -> dict[str, Any]:
        store, field_index, reranker = run.store, run.field_index, run.reranker
        store_start = len(store.calls)
        field_start = len(field_index.calls) if field_index is not None else 0
        rerank_start = len(reranker.calls) if reranker is not None else 0
        started = self._clock()
        try:
            # The spec preflight planned the first round from, so the queries it
            # provisioned are exactly the ones this run sends.
            result = run.session.run_spec(run.specs[question.case_id], current_section_index=0)
            context_ids = _pack_chunk_ids(result.context_pack)
            status = result.status.value
            decision = result.decision.reason
            transcript = result.transcript.to_dict()
            failure = (
                {
                    "type": "HarnessStatus",
                    "reason": str(result.decision.reason or result.status.value),
                }
                if result.status.value in {"failed", "halted"}
                else None
            )
        except Exception as exc:  # per-case failures remain visible and score as zero
            context_ids = []
            status = "system_failure"
            decision = "retrieval replay case failed"
            transcript = None
            failure = {"type": type(exc).__name__, "reason": str(exc)}

        provenance = [*store.calls[store_start:]]
        if field_index is not None:
            provenance.extend(field_index.calls[field_start:])
        candidate_ids = list(dict.fromkeys(
            candidate["chunk_id"]
            for call in provenance
            for candidate in call["results"]
        ))
        strict_candidate = _coverage(frozen_case, candidate_ids, strict=True)
        equivalent_candidate = _coverage(frozen_case, candidate_ids, strict=False)
        strict_context = _coverage(frozen_case, context_ids, strict=True)
        equivalent_context = _coverage(frozen_case, context_ids, strict=False)
        return {
            "profile": profile.name,
            "profile_fingerprint": profile.identity({
                "chunk_store": "CachedFixtureChunkStore",
                "field_index": _component_name(getattr(field_index, "_delegate", None)),
                "reranker": _component_name(getattr(reranker, "_delegate", None))
                    if profile.reranker_enabled else None,
                "field_reranker": _component_name(getattr(reranker, "_delegate", None))
                    if profile.field_reranker_enabled else None,
            })["fingerprint"],
            "case_id": question.case_id,
            "paper_id": question.paper_id,
            "question": question.question,
            "stratum": question.answer_type,
            "mapping_disposition": frozen_case.mapping_result.disposition,
            "status": status,
            "decision_reason": str(decision or ""),
            "failure": failure,
            "candidate_chunk_ids": candidate_ids,
            "context_chunk_ids": context_ids,
            "candidate_provenance": provenance,
            "candidate_coverage": {
                "strict": strict_candidate,
                "equivalent": equivalent_candidate,
            },
            "context_coverage": {
                "strict": strict_context,
                "equivalent": equivalent_context,
            },
            "reranker": {
                "available": reranker is not None,
                "called": bool(reranker and len(reranker.calls) > rerank_start),
                "call_count": (len(reranker.calls) - rerank_start) if reranker else 0,
            },
            "context_budget": {
                "max_items": profile.max_context_items,
                "max_tokens": profile.max_context_tokens,
                "actual_items": len(context_ids),
            },
            "transcript": transcript,
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        }

    def _profile_run(
        self,
        profile: QasperRetrievalReplayProfile,
        *,
        chunks: tuple[Any, ...],
        cache: OfflineEmbeddingCache,
        resolver: ReadOnlyQueryVectorResolver,
        field_index: FieldEmbeddingSearchPort | None,
        questions: Sequence[SampledQuestion],
    ) -> "_ProfileRun":
        """Build one profile's session and plan every case's first round."""
        store = _RecordingChunkStore(
            CachedFixtureChunkStore(chunks, cache, query_vector=resolver),
            resolver,
        )
        recording_field = (
            _RecordingFieldIndex(field_index, resolver)
            if field_index is not None else None
        )
        supplied_reranker = self._rerankers.get(profile.name)
        recording_reranker = _RecordingReranker(supplied_reranker) if supplied_reranker is not None else None
        session = PaperRAGSession(
            store,
            budget=replace(
                RAGBudget.safe_default(),
                max_context_items=profile.max_context_items,
                max_context_tokens=profile.max_context_tokens,
            ),
            field_index=recording_field,
            reranker=recording_reranker if profile.reranker_enabled else None,
            field_reranker=recording_reranker if profile.field_reranker_enabled else None,
            retrieval_policy=profile.policy,
            answer_worker=None,
            generation_policy={"enabled": False, "max_attempts": 0},
        )
        ask = AskPaperUseCase()
        graph_identity = build_paper_analysis_context_graph_identity(
            run_id=self._config.run_id, stage_id="run_research_rag",
        )
        specs: dict[str, RAGSessionSpec] = {}
        for index, question in enumerate(questions):
            session_id = f"qasper-replay-{profile.name}-{index:04d}"
            goal = ask.build_paper_ask_goal(
                paper_id=question.paper_id,
                question=question.question,
                goal_id=session_id,
            )
            specs[question.case_id] = session.build_spec(
                goal, graph_identity=graph_identity, session_id=session_id,
            )
        return _ProfileRun(
            session=session,
            store=store,
            field_index=recording_field,
            reranker=recording_reranker,
            specs=specs,
            initial_queries={
                case_id: session.initial_queries(spec) for case_id, spec in specs.items()
            },
        )

    def _profile_identity(
        self,
        profile: QasperRetrievalReplayProfile,
        *,
        field_index: FieldEmbeddingSearchPort | None = None,
    ) -> dict[str, Any]:
        reranker = self._rerankers.get(profile.name)
        components = {
            "chunk_store": "CachedFixtureChunkStore",
            "field_index": _component_name(field_index) if profile.field_index_enabled else None,
            "reranker": _component_name(reranker) if profile.reranker_enabled else None,
            "field_reranker": _component_name(reranker) if profile.field_reranker_enabled else None,
        }
        return profile.identity(components)

    def _publish(
        self,
        manifest: Mapping[str, Any],
        report: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        self._artifacts.publish(self._config.output_directory, {
            "manifest.json": _json_bytes(manifest),
            "report.json": _json_bytes(report),
            "cases.jsonl": b"".join(_json_bytes(row, newline=True) for row in rows),
        })


@dataclass(frozen=True)
class _PreparedReplay:
    assets: Any
    cache: OfflineEmbeddingCache
    resolver: ReadOnlyQueryVectorResolver
    questions: tuple[SampledQuestion, ...]
    frozen_by_id: Mapping[str, FrozenQasperCase]
    profile_runs: Mapping[str, "_ProfileRun"]


@dataclass(frozen=True)
class _ProfileRun:
    """One profile's session, its recording ports and every case's planned spec."""

    session: PaperRAGSession
    store: "_RecordingChunkStore"
    field_index: "_RecordingFieldIndex | None"
    reranker: "_RecordingReranker | None"
    specs: Mapping[str, RAGSessionSpec]
    initial_queries: Mapping[str, tuple[str, ...]]


class _RecordingChunkStore:
    def __init__(self, delegate: Any, resolver: ReadOnlyQueryVectorResolver) -> None:
        self._delegate = delegate
        self.resolver = resolver
        self.calls: list[dict[str, Any]] = []

    def get_chunk(self, chunk_id: str):
        return self._delegate.get_chunk(chunk_id)

    def list_chunks(self, paper_id: str):
        return self._delegate.list_chunks(paper_id)

    def search_with_scores(self, paper_id: str, query_text: str, *, filters=None, limit=10):
        self.resolver.set_channel("dense_chunk")
        try:
            results = self._delegate.search_with_scores(
                paper_id, query_text, filters=filters, limit=limit,
            )
        finally:
            self.resolver.set_channel(None)
        self.calls.append({
            "channel": "dense_chunk",
            "query": query_text,
            "limit": limit,
            "results": [
                {"chunk_id": chunk.chunk_id, "rank": rank, "score": score}
                for rank, (chunk, score) in enumerate(results, start=1)
            ],
        })
        return results


class _RecordingFieldIndex:
    def __init__(
        self,
        delegate: FieldEmbeddingSearchPort,
        resolver: ReadOnlyQueryVectorResolver,
    ) -> None:
        self._delegate = delegate
        self._resolver = resolver
        self.calls: list[dict[str, Any]] = []

    def search_field_vectors(self, paper_id: str, query_text: str, *, field_names=None, filters=None, limit=10):
        self._resolver.set_channel("field_embedding")
        try:
            results = self._delegate.search_field_vectors(
                paper_id, query_text, field_names=field_names, filters=filters, limit=limit,
            )
        finally:
            self._resolver.set_channel(None)
        self.calls.append({
            "channel": "field_embedding",
            "query": query_text,
            "limit": limit,
            "field_names": list(field_names) if field_names is not None else None,
            "results": [
                {
                    "chunk_id": hit.chunk_id,
                    "field_name": hit.field_name,
                    "rank": rank,
                    "score": hit.score,
                }
                for rank, hit in enumerate(results, start=1)
            ],
        })
        return results


class _RecordingReranker:
    def __init__(self, delegate: RerankerPort) -> None:
        self._delegate = delegate
        self.calls: list[dict[str, Any]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        scores = self._delegate.score(query, passages)
        self.calls.append({
            "query": query,
            "passage_count": len(passages),
            "score_count": len(scores),
        })
        return scores


def _verify_fixed_sample(
    raw_dataset: Mapping[str, Any],
    source_sample: Mapping[str, Any],
    answer_rows: Sequence[Mapping[str, Any]],
) -> tuple[SampledQuestion, ...]:
    seed = source_sample.get("seed")
    per_type = source_sample.get("per_type")
    if not isinstance(seed, str) or not seed.strip() or type(per_type) is not int or per_type < 1:
        raise QasperReplayPreflightError("source_sample_invalid", "source sample seed/per_type is invalid")
    regenerated = sample_questions(raw_dataset, seed=seed, per_type=per_type)
    expected_ids = tuple(str(case_id) for case_id in source_sample.get("case_ids") or ())
    actual_ids = tuple(question.case_id for question in regenerated.questions)
    if not expected_ids or expected_ids != actual_ids:
        raise QasperReplayPreflightError(
            "source_sample_mismatch", "source sample is not the deterministic sample for this dataset",
        )
    answer_ids = tuple(str(row.get("case_id") or "") for row in answer_rows)
    if answer_ids != expected_ids or len(set(answer_ids)) != len(answer_ids):
        raise QasperReplayPreflightError(
            "source_answers_mismatch", "source answers do not match the fixed sample identities and order",
        )
    return regenerated.questions


def _require_legacy_identity(identity: Mapping[str, Any], key: str, expected: Any) -> None:
    if key not in identity:
        raise QasperReplayPreflightError(
            "source_identity_missing", f"source report identity is missing required field {key}",
        )
    if identity[key] != expected:
        raise QasperReplayPreflightError(
            "source_identity_mismatch", f"source report {key} does not match the current frozen asset",
        )


def _match_optional_identity(identity: Mapping[str, Any], key: str, expected: Any) -> None:
    if key in identity and identity[key] != expected:
        raise QasperReplayPreflightError(
            "source_identity_mismatch", f"source report {key} does not match the current frozen asset",
        )


def _validate_optional_source_profile_fingerprint(identity: Mapping[str, Any]) -> None:
    fingerprint = identity.get("retrieval_profile_fingerprint")
    if fingerprint is None:
        return
    required = (
        "retrieval_profile",
        "retrieval_policy_settings",
        "retrieval_components",
        "context_budget",
    )
    if any(key not in identity for key in required):
        raise QasperReplayPreflightError(
            "source_identity_incomplete",
            "source retrieval profile fingerprint is present without its complete identity payload",
        )
    payload = {
        "profile": identity["retrieval_profile"],
        "policy": identity["retrieval_policy_settings"],
        "components": identity["retrieval_components"],
        "context_budget": identity["context_budget"],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    expected = hashlib.sha256(encoded).hexdigest()
    if fingerprint != expected:
        raise QasperReplayPreflightError(
            "source_identity_mismatch", "source retrieval profile fingerprint is inconsistent",
        )


def _coverage(case: FrozenQasperCase, chunk_ids: Sequence[str], *, strict: bool) -> dict[str, Any] | None:
    counts = evidence_group_coverage(case, chunk_ids, strict=strict)
    if counts is None:
        return None
    covered, total = counts
    return {"covered": covered, "denominator": total, "value": covered / total}


def _pack_chunk_ids(pack: Any | None) -> list[str]:
    if pack is None:
        return []
    return [
        str(item.metadata.get("rag_chunk_id") or item.evidence_id)
        for item in pack.accepted_evidence
    ]


def _profile_summary(
    profile: QasperRetrievalReplayProfile,
    rows: Sequence[Mapping[str, Any]],
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "name": profile.name,
        "identity": dict(identity),
        "case_ids_digest": content_digest([row["case_id"] for row in rows]),
        "cases": len(rows),
        "failures": sum(row["failure"] is not None for row in rows),
        "mapping_dispositions": _counts(row["mapping_disposition"] for row in rows),
        "reranker": {
            "available": any(row["reranker"]["available"] for row in rows),
            "called_cases": sum(row["reranker"]["called"] for row in rows),
            "calls": sum(row["reranker"]["call_count"] for row in rows),
        },
        "candidate_coverage": {
            "strict": _aggregate_coverage(rows, stage="candidate", mode="strict"),
            "equivalent": _aggregate_coverage(rows, stage="candidate", mode="equivalent"),
        },
        "context_coverage": {
            "strict": _aggregate_coverage(rows, stage="context", mode="strict"),
            "equivalent": _aggregate_coverage(rows, stage="context", mode="equivalent"),
        },
        "context_budget": {
            "max_items": profile.max_context_items,
            "max_tokens": profile.max_context_tokens,
            "max_actual_items": max((row["context_budget"]["actual_items"] for row in rows), default=0),
        },
    }


def _aggregate_coverage(
    rows: Sequence[Mapping[str, Any]], *, stage: str, mode: str,
) -> dict[str, Any]:
    values = [
        row[f"{stage}_coverage"][mode]
        for row in rows
        if row[f"{stage}_coverage"][mode] is not None
    ]
    micro_numerator = sum(value["covered"] for value in values)
    micro_denominator = sum(value["denominator"] for value in values)
    macro_numerator = sum(value["value"] for value in values)
    complete_cases = sum(value["covered"] == value["denominator"] for value in values)
    return {
        "measured_cases": len(values),
        "unmeasured_cases": len(rows) - len(values),
        "macro_case_fraction": {
            "value": macro_numerator / len(values) if values else None,
            "numerator": macro_numerator,
            "denominator": len(values),
        },
        "micro_evidence_groups": {
            "value": micro_numerator / micro_denominator if micro_denominator else None,
            "numerator": micro_numerator,
            "denominator": micro_denominator,
        },
        "complete_case_rate": {
            "value": complete_cases / len(values) if values else None,
            "numerator": complete_cases,
            "denominator": len(values),
        },
    }


def _counts(values: Sequence[str] | Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        text = str(value)
        counts[text] = counts.get(text, 0) + 1
    return dict(sorted(counts.items()))


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QasperReplayPreflightError(f"source_{label}_invalid", f"source {label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise QasperReplayPreflightError(f"source_{label}_invalid", f"source {label} must be a JSON object")
    return value


def _json_lines(payload: bytes, label: str) -> list[dict[str, Any]]:
    rows = []
    try:
        for line in payload.decode("utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("row is not an object")
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QasperReplayPreflightError(f"source_{label}_invalid", f"source {label} is invalid JSONL") from exc
    return rows


def _json_bytes(value: Mapping[str, Any], *, newline: bool = False) -> bytes:
    suffix = "\n" if newline else ""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")) + suffix).encode("utf-8")


def _component_name(component: object | None) -> str | None:
    if component is None:
        return None
    return f"{type(component).__module__}.{type(component).__qualname__}"


__all__ = [
    "FIXTURE_BOUNDARY",
    "PROTOCOL",
    "QasperReplayPreflightError",
    "QasperRetrievalReplayConfig",
    "QasperRetrievalReplayProfile",
    "QasperRetrievalReplayService",
    "ReadOnlyQueryVectorResolver",
]
