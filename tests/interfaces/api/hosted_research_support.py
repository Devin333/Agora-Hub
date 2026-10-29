"""Real research stack helpers for hosted API tests that run fully offline."""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from interfaces.services.research_desktop_control import canonical_request_fingerprint
from tests.interfaces.api.hosted_support import HOSTED_ORIGIN, configure_hosted_env


def configure_research_without_llm(monkeypatch, data_root: Path) -> None:
    """Point every research store at ``data_root`` and keep model calls off."""

    configure_hosted_env(monkeypatch, data_root)
    values = {
        "NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED": "0",
        "NEWS_PRELOAD_RERANKER": "1",
        "NEWS_RESEARCH_ROOT": str(data_root / "research"),
        "NEWS_RESEARCH_ARTIFACT_ROOT": str(data_root / "runs" / "artifacts"),
        "NEWS_ARTIFACT_ROOT": str(data_root / "runs" / "artifacts"),
        "NEWS_RESEARCH_RUN_STORE_ROOT": str(data_root / "runs" / "research"),
        "NEWS_RESEARCH_RAG_LOCAL_ROOT": str(data_root / "research" / "catalog" / "chunks"),
        "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT": str(data_root / "research" / "history" / "transcripts"),
        "NEWSROOM_RESEARCH_HISTORY_PATH": str(data_root / "research" / "history" / "history.json"),
        "NEWSROOM_RESEARCH_IMPORT_DIR": str(data_root / "research" / "imports"),
        "NEWSROOM_DESKTOP_CONTROL_PATH": str(data_root / "desktop" / "control" / "control.json"),
        "NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH": str(data_root / "research" / "personal-memory.sqlite3"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def forbid_network(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network access is forbidden in offline research API tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def content_fingerprint(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def selectable_text_pdf(marker: str, *, repeat: int = 10) -> bytes:
    """Build a three-section PDF whose text the local parser reads directly."""

    document = pymupdf.open()
    for index, heading in enumerate(("Introduction", "Method", "Results"), start=1):
        page = document.new_page()
        paragraph = (
            f"{heading}\n\n"
            f"{marker} section {index} validates deterministic local PDF import and reading. "
            "All content is embedded selectable text and requires no remote model, "
            "network source, vector database, or external parser process. "
        )
        page.insert_textbox(
            pymupdf.Rect(50, 60, 545, 790),
            paragraph * repeat,
            fontsize=10,
            lineheight=1.25,
        )
    payload = document.tobytes()
    document.close()
    return payload


def import_pdf(
    client: TestClient,
    *,
    headers: dict[str, str],
    generation: str,
    request_id: str,
    filename: str,
    source_revision: str,
    content: bytes,
) -> dict:
    """Submit one durable PDF import and return its passed operation."""

    source_fingerprint = content_fingerprint(content)
    semantic = {
        "kind": "import",
        "filename": filename,
        "sizeBytes": len(content),
        "sourceRevision": source_revision,
        "sourceFingerprint": source_fingerprint,
    }
    response = client.post(
        "/api/v1/research/desktop/operations/import",
        headers={
            **headers,
            "Origin": HOSTED_ORIGIN,
            "X-Newsroom-Request-Id": request_id,
            "X-Newsroom-Generation": generation,
            "X-Newsroom-Request-Fingerprint": canonical_request_fingerprint(semantic),
            "X-Newsroom-Source-Revision": source_revision,
            "X-Newsroom-Source-Fingerprint": source_fingerprint,
            "X-Filename": filename,
            "Content-Type": "application/pdf",
        },
        content=content,
    )
    assert response.status_code == 200, response.text
    operation = response.json()["data"]
    assert operation["status"] == "passed", operation
    return operation


__all__ = [
    "configure_research_without_llm",
    "content_fingerprint",
    "forbid_network",
    "import_pdf",
    "selectable_text_pdf",
]
