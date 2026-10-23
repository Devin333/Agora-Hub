"""Filesystem storage for supplementary QASPER query vectors (atomic NPZ replace)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np

from backend.research.ports.qasper_query_embedding import QueryVectorSnapshot

_FORMAT = "agorahub.qasper-query-vectors/v1"


class QasperQueryVectorStoreError(RuntimeError):
    """The supplementary query vector file is unreadable or malformed."""


class FilesystemQueryVectorStore:
    """Stores supplementary query vectors next to, never inside, the base cache."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    def load(self) -> QueryVectorSnapshot | None:
        if not self.path.is_file():
            return None
        try:
            with np.load(self.path, allow_pickle=False) as data:
                header = json.loads(str(data["header"]))
                keys = [str(key) for key in data["keys"].tolist()]
                matrix = np.asarray(data["vectors"], dtype=np.float32)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise QasperQueryVectorStoreError(f"invalid supplementary query vectors: {self.path}") from exc
        if header.get("format") != _FORMAT:
            raise QasperQueryVectorStoreError(f"unsupported supplementary query vector format: {header.get('format')!r}")
        if keys and (matrix.ndim != 2 or matrix.shape[0] != len(keys)):
            raise QasperQueryVectorStoreError("supplementary query vector keys and rows disagree")
        if len(set(keys)) != len(keys):
            raise QasperQueryVectorStoreError("supplementary query vectors contain duplicate keys")
        return QueryVectorSnapshot(
            base_cache_sha256=str(header["base_cache_sha256"]),
            model_id=str(header["model_id"]),
            dimension=int(header["dimension"]),
            vectors={key: tuple(float(x) for x in matrix[row]) for row, key in enumerate(keys)},
        )

    def save(self, snapshot: QueryVectorSnapshot) -> None:
        keys = sorted(snapshot.vectors)
        matrix = (
            np.asarray([snapshot.vectors[key] for key in keys], dtype=np.float32)
            if keys
            else np.empty((0, int(snapshot.dimension)), dtype=np.float32)
        )
        header = json.dumps(
            {
                "format": _FORMAT,
                "base_cache_sha256": snapshot.base_cache_sha256,
                "model_id": snapshot.model_id,
                "dimension": int(snapshot.dimension),
            },
            sort_keys=True,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=self.path.parent, prefix=".qasper-query-vectors-", suffix=".npz", delete=False
        ) as stream:
            temporary = Path(stream.name)
            np.savez(stream, header=np.asarray(header), keys=np.asarray(keys, dtype=str), vectors=matrix)
        try:
            os.replace(temporary, self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise


__all__ = ["FilesystemQueryVectorStore", "QasperQueryVectorStoreError"]
