from __future__ import annotations

import hashlib
import shutil
import socket
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pymupdf
import pytest

from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from interfaces.api import create_app
from interfaces.composition.research import (
    default_research_runtime_provider,
    reset_default_research_runtime,
)
from interfaces.services.auth_service import AuthApplicationService
from interfaces.services.research_desktop_control import canonical_request_fingerprint


_ORIGIN = "http://127.0.0.1:3000"
_OWNER = "desktop-owner"
_PASSWORD = "correct-horse-battery-staple"
_MEMORY_PATH = "/api/v1/research/desktop/memory"


def test_real_auth_session_reaches_disabled_research_desktop_and_local_stores(
    monkeypatch,
    short_desktop_data_root: tuple[Path, Path],
) -> None:
    data_root, import_root = short_desktop_data_root
    _configure_disabled_desktop(monkeypatch, data_root)
    monkeypatch.setenv("NEWSROOM_RESEARCH_IMPORT_DIR", str(import_root))

    preload_calls: list[str] = []

    def preload_reranker() -> None:
        preload_calls.append("preload")

    monkeypatch.setattr(
        "interfaces.services.paper_rag_factory.preload_reranker",
        preload_reranker,
    )

    auth = AuthApplicationService(
        user_store_path=data_root / "auth" / "users.json",
        session_store_path=data_root / "auth" / "sessions.json",
    )
    reset_default_research_runtime()
    app = create_app(
        auth_service_factory=lambda: auth,
        audit_emitter_factory=None,
    )

    try:
        with TestClient(app) as client, monkeypatch.context() as network_guard:
            _forbid_network(network_guard)
            bootstrap = client.post(
                "/api/v1/auth/bootstrap",
                headers={"Origin": _ORIGIN},
                json={"username": _OWNER, "password": _PASSWORD},
            )
            assert bootstrap.status_code == 200
            session = bootstrap.json()["data"]["session"]
            token = session["sessionToken"]
            user_id = session["user"]["userId"]
            headers = {"X-Newsroom-Session": token}

            history = client.get("/api/v1/research/history", headers=headers)
            assert history.status_code == 200
            assert history.json()["data"] == {
                "revision": 0,
                "visits": [],
                "groups": [],
            }

            identity = client.get(
                "/api/v1/research/desktop/identity",
                headers=headers,
            )
            assert identity.status_code == 200
            identity_data = identity.json()["data"]
            assert identity_data["owner"] == {
                "userId": user_id,
                "tenantId": None,
                "memoryNamespace": f"research:user:{user_id}",
            }
            assert identity_data["generation"].startswith("gen_")

            capabilities = client.get(
                "/api/v1/research/desktop/capabilities",
                headers=headers,
            )
            assert capabilities.status_code == 200
            assert (
                capabilities.json()["data"]["generation"]
                == identity_data["generation"]
            )

            policy = client.get(f"{_MEMORY_PATH}/policy", headers=headers)
            assert policy.status_code == 200
            policy_data = policy.json()["data"]["policy"]
            assert policy_data["allow_remote_processing"] is False
            assert policy_data["revision"] == 1
            assert policy_data["created_at"] == policy_data["updated_at"]

            created = client.post(
                _MEMORY_PATH,
                headers={**headers, "Origin": _ORIGIN},
                json={
                    "creation": "confirmed",
                    "request_id": "real-auth-memory-create",
                    "content": "Prefer concise, evidence-grounded summaries.",
                    "category": "reading_preference",
                    "source_refs": [
                        {
                            "source_type": "user_input",
                            "source_ref": "user://desktop/settings",
                        }
                    ],
                },
            )
            assert created.status_code == 200
            created_record = created.json()["data"]["record"]
            assert "owner" not in created_record
            assert created_record["status"] == "active"

            pdf = _selectable_text_pdf()
            source_fingerprint = f"sha256:{hashlib.sha256(pdf).hexdigest()}"
            source_revision = source_fingerprint
            semantic = {
                "kind": "import",
                "filename": "offline-paper.pdf",
                "sizeBytes": len(pdf),
                "sourceRevision": source_revision,
                "sourceFingerprint": source_fingerprint,
            }
            imported = client.post(
                "/api/v1/research/desktop/operations/import",
                headers={
                    **headers,
                    "Origin": _ORIGIN,
                    "X-Newsroom-Request-Id": "real-auth-pdf-import",
                    "X-Newsroom-Generation": identity_data["generation"],
                    "X-Newsroom-Request-Fingerprint": canonical_request_fingerprint(semantic),
                    "X-Newsroom-Source-Revision": source_revision,
                    "X-Newsroom-Source-Fingerprint": source_fingerprint,
                    "X-Filename": "offline-paper.pdf",
                    "Content-Type": "application/pdf",
                },
                content=pdf,
            )
            assert imported.status_code == 200, imported.text
            operation = imported.json()["data"]
            assert operation["status"] == "passed"
            paper_id = operation["result"]["paperId"]

            document = client.get(
                f"/api/v1/research/papers/{paper_id}/document",
                headers=headers,
            )
            assert document.status_code == 200
            document_data = document.json()["data"]["document"]
            assert document_data["status"] == "parsed"
            assert any("Introduction" in section["text"] for section in document_data["sections"])

            memories = client.get(_MEMORY_PATH, headers=headers)
            assert memories.status_code == 200
            assert memories.headers["cache-control"] == "private, no-store"
            assert [record["memory_id"] for record in memories.json()["data"]["records"]] == [
                created_record["memory_id"]
            ]

        composition = default_research_runtime_provider().get()
        assert composition.available is False
        assert composition.service.capability_projection()["parse"] is True
        assert composition.availability_error is not None
        assert composition.availability_error.to_public_dict()["capabilities"] == [
            "research.llm.route"
        ]
        # The warm-up runs beside the server so it never delays the listening socket.
        assert app.state.reranker_warmup.wait(timeout=10)
        assert preload_calls == ["preload"]
        assert storage_io_path(data_root / "desktop" / "control" / "control.json").is_file()
        assert storage_io_path(data_root / "research" / "personal-memory.sqlite3").is_file()
    finally:
        reset_default_research_runtime()



@pytest.fixture(params=("short", "candidate", "long-import"))
def short_desktop_data_root(request):
    if request.param == "short":
        root = resolve_storage_path(tempfile.mkdtemp(prefix="pa-api-"))
    else:
        root = resolve_storage_path(Path.cwd() / ".tmp" / "research-local" / uuid4().hex)
        storage_io_path(root).mkdir(parents=True, exist_ok=False)
    import_root = root / "research" / "imports"
    if request.param == "long-import":
        # Exercise this service's real long locator without making unrelated
        # auth/memory composition responsible for the import regression.
        import_root = import_root / ("desktop-data-" + "x" * 65) / ("owner-data-" + "y" * 65)
        assert len(str(import_root)) > 260
    print(f"IMPORT_PATH_CASE {request.param} data_root={root} import_root={import_root}")
    try:
        yield root, import_root
    finally:
        shutil.rmtree(storage_io_path(root))


def _configure_disabled_desktop(monkeypatch, data_root: Path) -> None:
    values = {
        "NEWSROOM_DEPLOYMENT_PROFILE": "local-desktop",
        "NEWSROOM_DESKTOP_CANONICAL_ORIGIN": _ORIGIN,
        "NEWSROOM_DESKTOP_INSTANCE_KEY": f"local-desktop:{_ORIGIN}",
        "NEWSROOM_DESKTOP_RELEASE_ID": "release-real-auth-test",
        "NEWSROOM_DESKTOP_CONFIG_DIGEST": "a" * 64,
        "NEWSROOM_DESKTOP_BOOT_ID": "b" * 32,
        "NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED": "0",
        "NEWS_PRELOAD_RERANKER": "1",
        "NEWS_RESEARCH_ROOT": str(data_root / "research"),
        "NEWS_RESEARCH_ARTIFACT_ROOT": str(data_root / "runs" / "artifacts"),
        "NEWS_ARTIFACT_ROOT": str(data_root / "runs" / "artifacts"),
        "NEWS_RESEARCH_RUN_STORE_ROOT": str(data_root / "runs" / "research"),
        "NEWS_RESEARCH_RAG_LOCAL_ROOT": str(data_root / "research" / "catalog" / "chunks"),
        "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT": str(
            data_root / "research" / "history" / "transcripts"
        ),
        "NEWSROOM_RESEARCH_HISTORY_PATH": str(
            data_root / "research" / "history" / "history.json"
        ),
        "NEWSROOM_RESEARCH_IMPORT_DIR": str(data_root / "research" / "imports"),
        "NEWSROOM_DESKTOP_CONTROL_PATH": str(
            data_root / "desktop" / "control" / "control.json"
        ),
        "NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH": str(
            data_root / "research" / "personal-memory.sqlite3"
        ),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NEWSROOM_CANONICAL_ORIGIN", raising=False)


def _forbid_network(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network access is forbidden in real desktop integration tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _selectable_text_pdf() -> bytes:
    document = pymupdf.open()
    for index, heading in enumerate(("Introduction", "Method", "Results"), start=1):
        page = document.new_page()
        paragraph = (
            f"{heading}\n\n"
            f"Section {index} validates deterministic local PDF import and reading. "
            "All content is embedded selectable text and requires no remote model, "
            "network source, vector database, or external parser process. "
        )
        page.insert_textbox(
            pymupdf.Rect(50, 60, 545, 790),
            paragraph * 12,
            fontsize=10,
            lineheight=1.25,
        )
    payload = document.tobytes()
    document.close()
    return payload

