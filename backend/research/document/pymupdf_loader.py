"""Deferred access to PyMuPDF.

PyMuPDF is a large native extension (about 0.6s to import). Parser modules are
imported by every process that composes the research stack, including the API
server, which never parses a PDF during startup. Modules that only touch PyMuPDF
while handling a document resolve it through :func:`load_pymupdf` at that point;
annotations import it under ``TYPE_CHECKING``.
"""

from __future__ import annotations

from types import ModuleType


def load_pymupdf() -> ModuleType:
    import pymupdf

    return pymupdf


__all__ = ["load_pymupdf"]
