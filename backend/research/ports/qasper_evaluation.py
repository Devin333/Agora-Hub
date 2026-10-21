"""Storage-only contracts for local QASPER inputs and immutable artifacts.

No document, retrieval, application or NumPy types cross this boundary. Cache
streams must be binary, seekable and closed when their context exits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, ContextManager, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class QasperRawSource:
    path: str
    content: bytes | None  # None denotes a deleted workspace file.


@dataclass(frozen=True)
class QasperRawCodeChange:
    status: str
    source: QasperRawSource


@dataclass(frozen=True)
class QasperRawCodeIdentity:
    git_revision: str
    sources: tuple[QasperRawSource, ...]
    changes: tuple[QasperRawCodeChange, ...]


class QasperAssetReader(Protocol):
    """Read explicit local storage inputs, without interpreting their business meaning."""

    def read_dataset(self) -> bytes: ...
    def open_cache(self) -> ContextManager[BinaryIO]: ...
    def read_code_identity(self, *, source_roots: Sequence[str]) -> QasperRawCodeIdentity: ...


class QasperArtifactStore(Protocol):
    def read(self, directory: str, name: str) -> bytes: ...
    def publish(self, directory: str, objects: Mapping[str, bytes]) -> None: ...
