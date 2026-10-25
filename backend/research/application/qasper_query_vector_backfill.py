"""Fill the supplementary query-vector cache from a blocked retrieval replay.

Retrieval replay never embeds: a replan or pipeline rewrite whose query text is
absent from the cache blocks the whole replay and lists the exact texts in its
manifest. This service embeds precisely those texts with the cache's own model,
so the next replay of the same configuration runs fully offline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.rag.evaluation.qasper_query_vectors import QueryVectorResolver
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache

_REPLAY_PROTOCOL = "qasper_retrieval_only_replay_v1"
_BACKFILLABLE_CODES = frozenset({"query_vectors_missing", "query_vectors_missing_during_replay"})


class QasperQueryVectorBackfillError(ValueError):
    """The named replay is not a query-vector block this cache can satisfy."""


def backfill_replay_query_vectors(
    artifacts: QasperArtifactStore,
    *,
    blocked_replay_directory: str,
    resolver: QueryVectorResolver,
    base_cache_sha256: str,
) -> dict[str, Any]:
    """Embed every query a blocked replay reported missing and persist the vectors."""
    report = _json_object(artifacts.read(blocked_replay_directory, "report.json"), "report.json")
    manifest = _json_object(artifacts.read(blocked_replay_directory, "manifest.json"), "manifest.json")
    if report.get("protocol") != _REPLAY_PROTOCOL or report.get("status") != "blocked":
        raise QasperQueryVectorBackfillError("source is not a blocked retrieval replay")
    blocked = report.get("blocked") or {}
    if blocked.get("code") not in _BACKFILLABLE_CODES:
        raise QasperQueryVectorBackfillError(
            f"replay was blocked by {blocked.get('code')!r}, which query vectors cannot fix"
        )
    identity = manifest.get("identity") or {}
    if identity.get("embedding_cache_sha256") not in (None, base_cache_sha256):
        raise QasperQueryVectorBackfillError("replay was measured against a different embedding cache")
    missing = manifest.get("missing_query_vectors")
    if not isinstance(missing, list) or not missing:
        raise QasperQueryVectorBackfillError("blocked replay lists no missing query vectors")

    # Validate every row before the first provider call, so a malformed manifest
    # never leaves a partially paid-for backfill behind.
    requested: list[tuple[str, str]] = []
    for row in missing:
        if not isinstance(row, Mapping) or not isinstance(row.get("query_text"), str):
            raise QasperQueryVectorBackfillError("missing_query_vectors rows must carry query_text")
        text = row["query_text"]
        digest = OfflineEmbeddingCache.key(text)
        if digest != row.get("query_digest"):
            raise QasperQueryVectorBackfillError("missing query text does not match its recorded digest")
        requested.append((text, digest))

    embedded: list[dict[str, Any]] = []
    already_present = 0
    for text, digest in requested:
        live_before = resolver.stats.live_calls
        resolver(text)
        if resolver.stats.live_calls > live_before:
            embedded.append({"query_digest": digest, "query_text": text})
        else:
            already_present += 1
    resolver.flush()
    return {
        "blocked_replay_directory": blocked_replay_directory,
        "requested": len(missing),
        "embedded": embedded,
        "already_present": already_present,
        "supplementary_size": resolver.supplementary_size,
    }


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise QasperQueryVectorBackfillError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise QasperQueryVectorBackfillError(f"{label} must be an object")
    return value


__all__ = ["QasperQueryVectorBackfillError", "backfill_replay_query_vectors"]
