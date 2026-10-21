"""Application-owned QASPER materialization, provenance and offline retrieval inputs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from zipfile import BadZipFile

import numpy as np

from backend.research.document.models import PaperChunk
from backend.research.domain.document import ResearchDocument
from backend.research.ports.qasper_evaluation import QasperAssetReader
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_execution import CodeIdentity
from backend.research.rag.evaluation.qasper_baseline import QasperBaselineCaptureError, _build_documents
from backend.research.rag.evaluation.qasper_retrieval import (
    CachedFixtureChunkStore,
    CachedFixtureFieldIndex,
    OfflineEmbeddingCache,
    QasperOfflineCacheError,
    RetrievalChannelInputs,
)

_CODE_ROOTS = ("backend", "framework", "infrastructure", "interfaces")


@dataclass(frozen=True)
class QasperAssets:
    raw_dataset: Mapping[str, Any]
    documents: tuple[ResearchDocument, ...]
    chunks: tuple[PaperChunk, ...]
    dataset_sha256: str
    code_identity: CodeIdentity


@dataclass(frozen=True)
class QasperCacheIdentity:
    sha256: str
    dimension: int
    model_id: str
    model_version: str


class QasperAssetProvider(Protocol):
    """Business inputs consumed by evaluation orchestration and its test doubles."""

    def read_assets(self) -> QasperAssets: ...
    def cache_identity(self) -> QasperCacheIdentity: ...
    def retrieval_inputs(self, chunks: Sequence[PaperChunk], *, dense: bool, field: bool) -> RetrievalChannelInputs: ...


class QasperAssetService:
    def __init__(
        self,
        reader: QasperAssetReader,
        *,
        embedding_model_id: str = "dashscope/text-embedding-v4",
        embedding_model_version: str = "text-embedding-v4",
    ) -> None:
        self._reader = reader
        self.embedding_model_id, self.embedding_model_version = embedding_model_id, embedding_model_version
        self._assets: QasperAssets | None = None
        self._cache: OfflineEmbeddingCache | None = None
        self._inputs: dict[tuple[tuple[str, ...], bool, bool], RetrievalChannelInputs] = {}
        self._stores: dict[tuple[tuple[str, ...], bool], object] = {}
        self._fields: dict[tuple[str, ...], CachedFixtureFieldIndex] = {}
        self._corpora: dict[tuple[str, ...], tuple[tuple[PaperChunk, ...], tuple[str, ...]]] = {}

    def read_assets(self) -> QasperAssets:
        if self._assets is None:
            try:
                payload = self._reader.read_dataset()
            except FileNotFoundError as exc:
                raise QasperBaselineCaptureError(f"QASPER dataset is missing: {exc.filename}") from exc
            except OSError as exc:
                raise QasperBaselineCaptureError(f"invalid QASPER dataset JSON: {exc}") from exc

            def reject_duplicate_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise QasperBaselineCaptureError(f"QASPER dataset contains duplicate JSON object key: {key!r}")
                    result[key] = value
                return result

            try:
                raw = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise QasperBaselineCaptureError(f"invalid QASPER dataset JSON: {exc}") from exc
            if not isinstance(raw, dict):
                raise QasperBaselineCaptureError("QASPER dataset must be a JSON object")
            documents, chunks = _build_documents(raw.get("papers", raw))
            self._assets = QasperAssets(
                raw, tuple(documents), tuple(chunks), hashlib.sha256(payload).hexdigest(),
                self.code_identity(),
            )
        return self._assets

    def code_identity(self) -> CodeIdentity:
        """Build business provenance from raw source bytes and git change records."""
        raw = self._reader.read_code_identity(source_roots=_CODE_ROOTS)
        entries = [(source.path, hashlib.sha256(source.content).hexdigest()) for source in raw.sources]
        mutable = [
            (change.status, change.source.path,
             hashlib.sha256(change.source.content).hexdigest() if change.source.content is not None else "deleted")
            for change in raw.changes
        ]
        return CodeIdentity.create(
            git_revision=raw.git_revision,
            workspace_content_fingerprint=content_digest(sorted(entries)),
            mutable_patch_fingerprints=(content_digest(sorted(mutable)),) if mutable else (),
            dirty=bool(mutable),
        )

    def _embedding_cache(self) -> OfflineEmbeddingCache:
        if self._cache is None:
            # Hash and decode the same open file, in bounded blocks; never buffer
            # the whole NPZ archive or expand its matrix into Python float lists.
            try:
                with self._reader.open_cache() as source:
                    digest = hashlib.sha256()
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                    source.seek(0)
                    try:
                        with np.load(source, allow_pickle=False) as data:
                            keys = [str(key) for key in data["keys"].tolist()]
                            matrix = np.asarray(data["vectors"], dtype=np.float32)
                    except (ValueError, KeyError, TypeError, EOFError, BadZipFile) as exc:
                        raise QasperOfflineCacheError(f"invalid embedding cache: {exc}") from exc
            except FileNotFoundError as exc:
                raise QasperOfflineCacheError(f"embedding cache does not exist: {exc.filename}") from exc
            except OSError as exc:
                raise QasperOfflineCacheError(f"invalid embedding cache: {exc}") from exc
            if matrix.ndim != 2 or len(keys) != matrix.shape[0] or matrix.shape[1] == 0:
                raise QasperOfflineCacheError("embedding cache keys/vectors have inconsistent shapes")
            if any(not key for key in keys) or len(set(keys)) != len(keys):
                raise QasperOfflineCacheError("embedding cache contains an empty or duplicate key")
            if not np.isfinite(matrix).all():
                raise QasperOfflineCacheError("embedding cache contains non-finite values")
            self._cache = OfflineEmbeddingCache(
                key_to_row={key: row for row, key in enumerate(keys)}, matrix=matrix, cache_id=digest.hexdigest(),
            )
        return self._cache

    def cache_identity(self) -> QasperCacheIdentity:
        cache = self._embedding_cache()
        return QasperCacheIdentity(
            cache.cache_id, int(cache.matrix.shape[1]), self.embedding_model_id, self.embedding_model_version,
        )

    def retrieval_inputs(
        self, chunks: Sequence[PaperChunk], *, dense: bool, field: bool,
    ) -> RetrievalChannelInputs:
        # Compare against a deep snapshot before reusing a corpus signature.
        # IDs alone miss content/metadata changes; serializing the whole corpus
        # on every question is needlessly expensive for the normal frozen run.
        chunk_ids = tuple(chunk.chunk_id for chunk in chunks)
        cached = self._corpora.get(chunk_ids)
        if cached is None or tuple(chunks) != cached[0]:
            snapshot = tuple(chunk.model_copy(deep=True) for chunk in chunks)
            signature = tuple(json.dumps(chunk.model_dump(mode="json"), sort_keys=True) for chunk in snapshot)
            self._corpora[chunk_ids] = (snapshot, signature)
        else:
            snapshot, signature = cached
        key = (signature, dense, field)
        if key not in self._inputs:
            store_key = (signature, dense)
            if store_key not in self._stores:
                # Sparse-only runs do not inspect embeddings at all.
                self._stores[store_key] = (
                    CachedFixtureChunkStore(snapshot, self._embedding_cache()) if dense else _ReadOnlyChunks(snapshot)
                )
            if field and signature not in self._fields:
                self._fields[signature] = CachedFixtureFieldIndex(snapshot, self._embedding_cache())
            self._inputs[key] = RetrievalChannelInputs(
                self._stores[store_key], self._fields[signature] if field else None,
            )
        return self._inputs[key]


class _ReadOnlyChunks:
    def __init__(self, chunks: Sequence[PaperChunk]) -> None:
        self.chunks: dict[str, PaperChunk] = {}
        self._by_paper: dict[str, list[PaperChunk]] = {}
        for chunk in chunks:
            if chunk.chunk_id in self.chunks:
                raise ValueError(f"duplicate fixture chunk id: {chunk.chunk_id}")
            self.chunks[chunk.chunk_id] = chunk
            self._by_paper.setdefault(chunk.paper_id, []).append(chunk)

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        return list(self._by_paper.get(paper_id, ()))

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        return self.chunks.get(chunk_id)
