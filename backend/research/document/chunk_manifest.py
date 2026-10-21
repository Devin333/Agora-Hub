from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from datetime import UTC, datetime
from collections.abc import Iterator, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from backend.research.document.models import PaperChunk
from backend.research.document.citation_spans import remap_span_origin_ids
from framework.rag.core import build_chunk_semantic_key, build_rag_stable_id, content_fingerprint
from framework.shared.windows_paths import resolve_storage_path, storage_io_path


_MANIFEST_VERSION = 2
_MANIFEST_LOCK_TIMEOUT_SECONDS = 30.0
_MANIFEST_LOCK_POLL_SECONDS = 0.05
_MANIFEST_PATH_LOCKS_GUARD = threading.Lock()
_MANIFEST_PATH_LOCKS: dict[Path, threading.RLock] = {}


def default_chunk_manifest_path(paper_id: str) -> Path:
    root = Path(os.environ.get("NEWS_ARTIFACT_ROOT", ".newsroom/runs"))
    return root.parent / "papers" / paper_id / "chunk_manifest.json"


class ChunkManifestManager:
    def __init__(self, manifest_path: str | Path | None = None) -> None:
        self._manifest_path = (resolve_storage_path(manifest_path) if manifest_path is not None else None)

    def path_for(
        self,
        paper_id: str,
        *,
        actor_scope: Mapping[str, str] | None = None,
    ) -> Path:
        paper_id = _safe_paper_id(paper_id)
        scope_key = _scope_key(actor_scope)
        if self._manifest_path is None:
            base = default_chunk_manifest_path(paper_id)
            if scope_key != "public":
                return base.parent.parent / scope_key / paper_id / base.name
            return base
        if self._manifest_path.suffix.casefold() == ".json":
            if scope_key == "public":
                return self._manifest_path
            return self._manifest_path.with_name(
                f"{self._manifest_path.stem}-{scope_key}{self._manifest_path.suffix}"
            )
        root = self._manifest_path / scope_key if scope_key != "public" else self._manifest_path
        return root / paper_id / "chunk_manifest.json"

    def is_current(
        self,
        paper_id: str,
        *,
        document_id: str,
        source_hash: str | None,
        pipeline_fingerprint: str | None,
        actor_scope: Mapping[str, str] | None = None,
    ) -> bool:
        """Return whether the committed projection matches the complete input identity."""
        if not pipeline_fingerprint or not source_hash:
            return False
        manifest = self._load(paper_id, actor_scope=actor_scope)
        return (
            manifest.get("document_id") == document_id
            and manifest.get("source_hash") == source_hash
            and manifest.get("pipeline_fingerprint") == pipeline_fingerprint
            and manifest.get("version") == _MANIFEST_VERSION
        )

    def resolve_chunk_ids(
        self,
        paper_id: str,
        chunks: list[PaperChunk],
        *,
        actor_scope: Mapping[str, str] | None = None,
    ) -> list[PaperChunk]:
        if not chunks:
            return []
        manifest = self._load(paper_id, actor_scope=actor_scope)
        previous_by_key = _previous_chunk_ids_by_semantic_key(manifest)
        previous_ids = set(previous_by_key.values())
        keyed_chunks = _ensure_unique_semantic_keys(paper_id, chunks)
        old_id_counts: dict[str, int] = {}
        for chunk in keyed_chunks:
            old_id_counts[chunk.chunk_id] = old_id_counts.get(chunk.chunk_id, 0) + 1

        id_map: dict[str, str] = {}
        resolved_pairs: list[tuple[PaperChunk, str]] = []
        used_ids: set[str] = set()
        for chunk in keyed_chunks:
            semantic_key = str(chunk.metadata.get("semantic_key") or "")
            old_id = chunk.chunk_id
            resolved_id = previous_by_key.get(semantic_key)
            if resolved_id is None:
                resolved_id = (
                    build_rag_stable_id("chunk", paper_id, "new", semantic_key)
                    if old_id in previous_ids
                    else old_id
                )
            if resolved_id in used_ids:
                if old_id not in used_ids and old_id not in previous_ids:
                    resolved_id = old_id
                else:
                    resolved_id = build_rag_stable_id(
                        "chunk",
                        paper_id,
                        "manifest",
                        semantic_key,
                        old_id,
                    )
                    collision_index = 2
                    while resolved_id in used_ids:
                        resolved_id = build_rag_stable_id(
                            "chunk",
                            paper_id,
                            "manifest",
                            semantic_key,
                            old_id,
                            str(collision_index),
                        )
                        collision_index += 1
            if old_id_counts.get(old_id, 0) == 1:
                id_map[old_id] = resolved_id
            resolved_pairs.append((chunk, resolved_id))
            used_ids.add(resolved_id)

        return [
            _remap_chunk_identity(chunk, resolved_id, id_map)
            for chunk, resolved_id in resolved_pairs
        ]

    def write(
        self,
        paper_id: str,
        chunks: list[PaperChunk],
        *,
        document_metadata: dict[str, Any] | None = None,
        actor_scope: Mapping[str, str] | None = None,
        document_id: str | None = None,
        source_hash: str | None = None,
        run_id: str | None = None,
        observed_at: datetime | str | None = None,
    ) -> Path:
        path = self.path_for(paper_id, actor_scope=actor_scope)
        with _manifest_path_lock(path):
            storage_io_path(path.parent).mkdir(parents=True, exist_ok=True)
            previous = self._load(paper_id, actor_scope=actor_scope)
            current_keys = {
                str(chunk.metadata.get("semantic_key"))
                for chunk in chunks
                if chunk.metadata.get("semantic_key")
            }
            stale_chunk_ids = [
                str(entry.get("chunk_id"))
                for entry in previous.get("chunks", [])
                if entry.get("semantic_key") and entry.get("semantic_key") not in current_keys
            ]
            metadata = dict(document_metadata or {})
            document_id = str(document_id or metadata.get("document_id") or paper_id)
            source_hash = str(source_hash or metadata.get("source_hash") or "") or None
            scope = _normalized_scope(actor_scope or metadata.get("actor_scope"))
            observed_text = _iso_timestamp(observed_at)
            previous_manifest_id = str(previous.get("manifest_id") or "") or None
            pipeline_fingerprint = str(metadata.get("parse_cache_fingerprint") or "") or None
            manifest_id = _manifest_id(
                paper_id, document_id, source_hash, pipeline_fingerprint, observed_text
            )
            previous_entries = [entry for entry in previous.get("chunks", []) if isinstance(entry, dict)]
            entries = [
                _manifest_entry(
                    chunk,
                    paper_id=paper_id,
                    document_id=document_id,
                    source_hash=source_hash,
                    actor_scope=scope,
                    previous_entries=previous_entries,
                )
                for chunk in chunks
            ]
            history = [item for item in previous.get("history", []) if isinstance(item, dict)]
            if previous_manifest_id and previous_manifest_id != manifest_id:
                history.append(
                    {
                        "manifest_id": previous_manifest_id,
                        "document_id": previous.get("document_id"),
                        "source_hash": previous.get("source_hash"),
                        "pipeline_fingerprint": previous.get("pipeline_fingerprint"),
                        "observed_at": previous.get("observed_at"),
                        "run_id": previous.get("run_id"),
                        "chunks": previous_entries,
                        "stale_chunk_ids": list(previous.get("stale_chunk_ids", [])),
                    }
                )
            payload = {
                "version": _MANIFEST_VERSION,
                "manifest_id": manifest_id,
                "paper_id": paper_id,
                "document_id": document_id,
                "source_hash": source_hash,
                "pipeline_fingerprint": pipeline_fingerprint,
                "observed_at": observed_text,
                "run_id": str(run_id).strip() if run_id else None,
                "actor_scope": scope,
                "chunks": entries,
                "stale_chunk_ids": stale_chunk_ids,
                "history": history,
                **_parser_cascade_manifest(document_metadata),
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            _atomic_write_json(path, payload)
            return path

    def _load(
        self,
        paper_id: str,
        *,
        actor_scope: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        path = self.path_for(paper_id, actor_scope=actor_scope)
        io_path = storage_io_path(path)
        if not io_path.exists():
            return {"version": _MANIFEST_VERSION, "paper_id": paper_id, "chunks": []}
        try:
            payload = json.loads(io_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": _MANIFEST_VERSION, "paper_id": paper_id, "chunks": []}
        if not isinstance(payload, dict):
            return {"version": _MANIFEST_VERSION, "paper_id": paper_id, "chunks": []}
        chunks = payload.get("chunks")
        if not isinstance(chunks, list):
            payload["chunks"] = []
        return payload


def _previous_chunk_ids_by_semantic_key(manifest: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in manifest.get("chunks", []):
        if not isinstance(entry, dict):
            continue
        semantic_key = entry.get("semantic_key")
        chunk_id = entry.get("chunk_id")
        if semantic_key and chunk_id and semantic_key not in result:
            result[str(semantic_key)] = str(chunk_id)
    return result


def _ensure_unique_semantic_keys(paper_id: str, chunks: list[PaperChunk]) -> list[PaperChunk]:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    counts: dict[str, int] = {}
    keyed: list[PaperChunk] = []
    for chunk in chunks:
        metadata = _ensure_semantic_metadata(paper_id, chunk, by_id)
        base_key = str(metadata["semantic_key"])
        occurrence = counts.get(base_key, 0) + 1
        counts[base_key] = occurrence
        if occurrence > 1:
            metadata["semantic_key_base"] = base_key
            metadata["semantic_key_occurrence"] = occurrence
            metadata["semantic_key"] = f"{base_key}#{occurrence}"
        else:
            metadata["semantic_key_occurrence"] = occurrence
        keyed.append(chunk.model_copy(update={"metadata": metadata}))
    return keyed


def _ensure_semantic_metadata(
    paper_id: str,
    chunk: PaperChunk,
    chunks_by_id: dict[str, PaperChunk],
) -> dict[str, Any]:
    metadata = dict(chunk.metadata)
    parent = chunks_by_id.get(chunk.parent_chunk_id or "")
    source_chunk = chunks_by_id.get(str(metadata.get("source_chunk_id") or ""))
    anchor = parent or source_chunk
    source_ref = str(
        metadata.get("source_ref")
        or (anchor.metadata.get("source_ref") if anchor else "")
        or f"paper://{paper_id}"
    )
    source_locator = str(
        metadata.get("source_locator")
        or (anchor.metadata.get("source_locator") if anchor else "")
        or source_ref
    )
    content_hash = str(metadata.get("content_hash") or content_fingerprint(chunk.content))
    semantic_key = metadata.get("semantic_key")
    if not semantic_key:
        semantic = build_chunk_semantic_key(
            document_id=paper_id,
            chunk_type=chunk.chunk_type,
            section_title=chunk.section_title or (anchor.section_title if anchor else ""),
            source_locator=source_locator,
            content=chunk.content,
            content_hash=content_hash,
        )
        semantic_key = semantic.key
        content_hash = semantic.content_hash
        metadata["semantic_key_parts"] = dict(semantic.parts)
    metadata["source_ref"] = source_ref
    metadata["source_locator"] = source_locator
    metadata["content_hash"] = content_hash
    metadata["semantic_key"] = str(semantic_key)
    return metadata


def _remap_chunk_identity(
    chunk: PaperChunk,
    resolved_id: str,
    id_map: dict[str, str],
) -> PaperChunk:
    metadata = dict(chunk.metadata)
    metadata = remap_span_origin_ids(metadata, id_map)
    updates: dict[str, Any] = {}
    if resolved_id != chunk.chunk_id:
        metadata["generated_chunk_id"] = chunk.chunk_id
        metadata["chunk_id_reused_from_manifest"] = True
        updates["chunk_id"] = resolved_id
    else:
        metadata.setdefault("chunk_id_reused_from_manifest", False)

    if chunk.parent_chunk_id and chunk.parent_chunk_id in id_map:
        updates["parent_chunk_id"] = id_map[chunk.parent_chunk_id]

    source_chunk_id = metadata.get("source_chunk_id")
    if source_chunk_id is not None:
        source_chunk_id_text = str(source_chunk_id)
        if source_chunk_id_text in id_map:
            metadata["source_chunk_id"] = id_map[source_chunk_id_text]

    remapped_refs = [id_map.get(ref, ref) for ref in chunk.references]
    if remapped_refs != chunk.references:
        updates["references"] = remapped_refs

    updates["metadata"] = metadata
    return chunk.model_copy(update=updates)


def _manifest_entry(
    chunk: PaperChunk,
    *,
    paper_id: str | None = None,
    document_id: str | None = None,
    source_hash: str | None = None,
    actor_scope: Mapping[str, str] | None = None,
    previous_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata = dict(chunk.metadata)
    locators = metadata.get("source_locators")
    if isinstance(locators, str):
        locators = [locators]
    elif not isinstance(locators, (list, tuple)):
        locators = []
    locators = _unique_texts([
        *[str(item) for item in locators],
        metadata.get("source_locator"),
        metadata.get("source_ref"),
    ])
    previous = _previous_entry_for_chunk(chunk, previous_entries or [])
    current_hash = str(metadata.get("content_hash") or content_fingerprint(chunk.content))
    stale_of = None
    if previous is not None and str(previous.get("content_hash") or "") != current_hash:
        stale_of = str(previous.get("chunk_id") or "") or None
    return {
        "chunk_id": chunk.chunk_id,
        "paper_id": paper_id or chunk.paper_id,
        "document_id": document_id or metadata.get("document_id") or paper_id or chunk.paper_id,
        "source_hash": source_hash or metadata.get("source_hash"),
        "semantic_key": chunk.metadata.get("semantic_key"),
        "content_hash": current_hash,
        "content_ref": str(metadata.get("content_ref") or f"chunk://{paper_id or chunk.paper_id}/{chunk.chunk_id}"),
        "chunk_type": chunk.chunk_type,
        "parent_chunk_id": chunk.parent_chunk_id,
        "section_title": chunk.section_title,
        "section_index": chunk.section_index,
        "source_locator": chunk.metadata.get("source_locator"),
        "source_ref": chunk.metadata.get("source_ref"),
        "source_locators": locators,
        "parse_source": chunk.parse_source,
        "stale_of": stale_of,
        "actor_scope": dict(actor_scope or {}),
    }


def _previous_entry_for_chunk(
    chunk: PaperChunk,
    previous_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    semantic_key = str(chunk.metadata.get("semantic_key") or "")
    source_locator = str(chunk.metadata.get("source_locator") or "")
    for entry in previous_entries:
        if semantic_key and str(entry.get("semantic_key") or "") == semantic_key:
            return entry
    for entry in previous_entries:
        if source_locator and str(entry.get("source_locator") or "") == source_locator:
            return entry
    return None


def _manifest_id(
    paper_id: str,
    document_id: str,
    source_hash: str | None,
    pipeline_fingerprint: str | None,
    observed_at: str,
) -> str:
    encoded = "|".join((paper_id, document_id, source_hash or "", pipeline_fingerprint or "", observed_at))
    return "manifest:" + sha256(encoded.encode("utf-8")).hexdigest()


def _iso_timestamp(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat()
    if value:
        return str(value)
    return datetime.now(UTC).isoformat()


def _normalized_scope(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("actor_scope")
    # Callers may pass either the typed scope itself or a metadata envelope
    # containing ``actor_scope``. Flatten both forms before deriving a path so
    # a scoped manifest can never fall back to the public location.
    source: dict[str, Any] = dict(value)
    if isinstance(nested, Mapping):
        source.update(dict(nested))
    allowed = {"tenant_id", "user_id", "memory_namespace"}
    return {
        str(key): str(raw).strip()
        for key, raw in source.items()
        if str(key) in allowed and str(raw).strip()
    }


def _unique_texts(values: list[Any] | tuple[Any, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _parser_cascade_manifest(document_metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not document_metadata:
        return {}
    cascade = document_metadata.get("parser_cascade")
    if not isinstance(cascade, dict):
        return {}
    return {"parser_cascade": cascade}


def _scope_key(scope: Mapping[str, str] | None) -> str:
    values = _normalized_scope(scope)
    if not values:
        return "public"
    encoded = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _safe_paper_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or any(char in text for char in "\\/:\x00"):
        raise ValueError("paper_id contains an unsafe path segment")
    return text


@contextmanager
def _manifest_path_lock(path: Path) -> Iterator[None]:
    """Serialize one manifest load/merge/replace across threads and processes."""

    logical_path = resolve_storage_path(path)
    lock_path = logical_path.with_name(f"{logical_path.name}.lock")
    io_lock_path = storage_io_path(lock_path)
    storage_io_path(logical_path.parent).mkdir(parents=True, exist_ok=True)
    with _path_lock(logical_path):
        with io_lock_path.open("a+b") as handle:
            _lock_handle(handle)
            try:
                yield
            finally:
                _unlock_handle(handle)


def _path_lock(path: Path) -> threading.RLock:
    with _MANIFEST_PATH_LOCKS_GUARD:
        return _MANIFEST_PATH_LOCKS.setdefault(path, threading.RLock())


def _lock_handle(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + _MANIFEST_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out acquiring the chunk manifest lock") from None
                time.sleep(_MANIFEST_LOCK_POLL_SECONDS)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace a manifest atomically so an interrupted refresh cannot truncate it."""

    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary: str | None = None
    io_path = storage_io_path(path)
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=storage_io_path(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, io_path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


__all__ = ["ChunkManifestManager", "default_chunk_manifest_path"]
