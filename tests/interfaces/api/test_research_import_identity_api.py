from __future__ import annotations

import hashlib
import json
import shutil
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pymupdf
import pytest

from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from interfaces.api import create_app
from interfaces.composition.research import reset_default_research_runtime
from interfaces.services.research_desktop_control import canonical_request_fingerprint
from tests.interfaces.api.hosted_support import (
    HOSTED_ORIGIN,
    build_test_hosted_runtime,
    configure_hosted_env,
    sign_in,
)


_ORIGIN = HOSTED_ORIGIN
_PASSWORD = "correct-horse-battery-staple"


def test_pdf_import_api_uses_owner_scoped_content_identity_across_paths_and_restart(
    monkeypatch,
    short_api_root: tuple[Path, Path],
) -> None:
    data_root, import_root = short_api_root
    _configure_research_without_llm(monkeypatch, data_root)
    monkeypatch.setenv("NEWSROOM_RESEARCH_IMPORT_DIR", str(import_root))
    monkeypatch.setattr("interfaces.services.paper_rag_factory.preload_reranker", lambda: None)
    hosted_runtime = build_test_hosted_runtime(data_root)
    alice_headers = sign_in(hosted_runtime, "alice", _PASSWORD)
    bob_headers = sign_in(hosted_runtime, "bob", _PASSWORD)
    reset_default_research_runtime()
    app = create_app(hosted_runtime=hosted_runtime, audit_emitter_factory=None)
    first_pdf = _selectable_text_pdf("stable-api-content")
    different_pdf = _selectable_text_pdf("different-api-content")
    first_fingerprint = _fingerprint(first_pdf)

    try:
        with TestClient(app) as client, monkeypatch.context() as network_guard:
            _forbid_network(network_guard)
            generation = client.get(
                "/api/v1/research/desktop/identity",
                headers=alice_headers,
            ).json()["data"]["generation"]

            first = _import_pdf(
                client,
                headers=alice_headers,
                generation=generation,
                request_id="identity-first",
                filename="original.pdf",
                source_revision="revision-first",
                content=first_pdf,
            )
            renamed = _import_pdf(
                client,
                headers=alice_headers,
                generation=generation,
                request_id="identity-renamed",
                filename="renamed.pdf",
                source_revision="revision-renamed",
                content=first_pdf,
            )
            different = _import_pdf(
                client,
                headers=alice_headers,
                generation=generation,
                request_id="identity-different",
                filename="original.pdf",
                source_revision="revision-different",
                content=different_pdf,
            )
            other_owner = _import_pdf(
                client,
                headers=bob_headers,
                generation=generation,
                request_id="identity-other-owner",
                filename="original.pdf",
                source_revision="revision-bob",
                content=first_pdf,
            )

            index = json.loads(storage_io_path(import_root / "index.json").read_text(encoding="utf-8"))
            for record in index["imports"].values():
                locator = record["path"]
                assert not locator.startswith("\\\\?\\")
                assert Path(locator).is_relative_to(import_root)
                assert storage_io_path(locator).is_file()
                assert not list(storage_io_path(locator).parent.glob("*.tmp"))

            paper_id = first["result"]["paperId"]
            assert renamed["result"]["paperId"] == paper_id
            assert different["result"]["paperId"] != paper_id
            assert other_owner["result"]["paperId"] != paper_id

            readable = client.get(
                f"/api/v1/research/papers/{paper_id}/document",
                headers=alice_headers,
            )
            assert readable.status_code == 200
            assert readable.json()["data"]["document"]["status"] == "parsed"

            # Knowing another owner's paper id, revision, fingerprint, and source
            # locator contract must not cross the authenticated import authority.
            attacked_document = client.get(
                f"/api/v1/research/papers/{paper_id}/document",
                headers=bob_headers,
            )
            assert attacked_document.status_code == 404
            attacked_source = client.get(
                f"/api/v1/research/desktop/papers/{paper_id}/source",
                headers=bob_headers,
                params={
                    "sourceRevision": "revision-first",
                    "sourceFingerprint": first_fingerprint,
                },
            )
            assert attacked_source.status_code == 404

        reset_default_research_runtime()
        # A restarted server reads the same account store, so the session
        # issued before the restart still identifies alice.
        reopened_app = create_app(
            hosted_runtime=build_test_hosted_runtime(data_root),
            audit_emitter_factory=None,
        )
        with TestClient(reopened_app) as reopened_client, monkeypatch.context() as network_guard:
            _forbid_network(network_guard)
            reopened_generation = reopened_client.get(
                "/api/v1/research/desktop/identity",
                headers=alice_headers,
            ).json()["data"]["generation"]
            after_restart = _import_pdf(
                reopened_client,
                headers=alice_headers,
                generation=reopened_generation,
                request_id="identity-after-restart",
                filename="after-restart.pdf",
                source_revision="revision-after-restart",
                content=first_pdf,
            )
            assert after_restart["result"]["paperId"] == paper_id
            source = reopened_client.get(
                f"/api/v1/research/desktop/papers/{paper_id}/source",
                headers=alice_headers,
                params={
                    "sourceRevision": "revision-after-restart",
                    "sourceFingerprint": first_fingerprint,
                },
            )
            assert source.status_code == 200
            assert source.content == first_pdf
            assert "after-restart.pdf" in source.headers["content-disposition"]
    finally:
        reset_default_research_runtime()


def _import_pdf(
    client: TestClient,
    *,
    headers: dict[str, str],
    generation: str,
    request_id: str,
    filename: str,
    source_revision: str,
    content: bytes,
) -> dict:
    source_fingerprint = _fingerprint(content)
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
            "Origin": _ORIGIN,
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


@pytest.fixture(params=("short", "candidate", "long-import"))
def short_api_root(request):
    if request.param == "short":
        root = resolve_storage_path(tempfile.mkdtemp(prefix="pa-api-"))
    else:
        root = resolve_storage_path(Path.cwd() / ".tmp" / "research-identity-api" / uuid4().hex)
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


def _configure_research_without_llm(monkeypatch, data_root: Path) -> None:
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


def _forbid_network(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network access is forbidden in import identity API tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _fingerprint(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _selectable_text_pdf(marker: str) -> bytes:
    document = pymupdf.open()
    for index, heading in enumerate(("Introduction", "Method", "Results"), start=1):
        page = document.new_page()
        paragraph = (
            f"{heading}\n\n"
            f"{marker} section {index} validates deterministic local PDF import and reading. "
            "All content is selectable and stays inside the local Research runtime. "
        )
        page.insert_textbox(
            pymupdf.Rect(50, 60, 545, 790),
            paragraph * 10,
            fontsize=10,
            lineheight=1.25,
        )
    payload = document.tobytes()
    document.close()
    return payload


@pytest.mark.parametrize("failure_stage", ("write", "replace"))
@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_import_temporary_failure_preserves_original_exception_and_owned_cleanup(
    monkeypatch, tmp_path: Path, failure_stage: str, cleanup_fails: bool,
) -> None:
    from interfaces.services.research_import_service import ResearchImportService

    imports = ResearchImportService(tmp_path / "imports")
    import_id = "imp_" + "a" * 64
    original = OSError("original PDF " + failure_stage + " failure")
    real_open, real_replace, real_unlink = Path.open, Path.replace, Path.unlink
    created: list[Path] = []
    cleanup: list[Path] = []

    class FailingWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def write(self, content):
            self.handle.write(content[:5])
            raise original

    def open_file(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if path.name.startswith(import_id + ".") and mode == "xb":
            created.append(path)
            if failure_stage == "write":
                return FailingWriter(handle)
        return handle

    def replace_file(path, target):
        if path in created:
            raise original
        return real_replace(path, target)

    def unlink_file(path, *args, **kwargs):
        if path in created:
            cleanup.append(path)
            if cleanup_fails:
                raise PermissionError("PDF cleanup denied")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_file)
        patch.setattr(Path, "replace", replace_file)
        patch.setattr(Path, "unlink", unlink_file)
        with pytest.raises(OSError) as failure:
            imports.create_received(user_id="alice", import_id=import_id, content=b"%PDF-real")
    assert failure.value is original
    assert len(created) == 1 and cleanup == created
    assert created[0].is_file() is cleanup_fails
    assert not (imports.root / "alice" / (import_id + ".pdf")).exists()
    assert not imports.index_path.exists()
    if cleanup_fails:
        assert any("PDF cleanup denied" in note for note in original.__notes__)
        created[0].unlink()


def test_import_temporary_collision_does_not_overwrite_or_remove_unowned_file(monkeypatch, tmp_path: Path) -> None:
    from interfaces.services import research_import_service as module

    imports = module.ResearchImportService(tmp_path / "imports")
    import_id = "imp_" + "b" * 64
    token = "c" * 32
    temporary = storage_io_path(imports.root / "alice" / f"{import_id}.{token}.tmp")
    temporary.parent.mkdir(parents=True)
    temporary.write_bytes(b"another writer's PDF")
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex=token))
    with pytest.raises(FileExistsError):
        imports.create_received(user_id="alice", import_id=import_id, content=b"%PDF-new")
    assert temporary.read_bytes() == b"another writer's PDF"
    assert not (temporary.parent / (import_id + ".pdf")).exists()
    assert not imports.index_path.exists()
