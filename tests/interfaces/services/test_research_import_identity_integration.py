from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import shutil
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from infrastructure.research.catalog_store import FilesystemResearchCatalogStore
from interfaces.composition.research_local import build_local_research_service
from interfaces.composition.research_settings import ResearchLocalRuntimeSettings
from interfaces.services.research_import_application import ResearchImportApplication
from interfaces.services.research_import_service import ResearchImportService


def test_real_local_import_identity_is_content_stable_owner_scoped_and_durable(short_identity_root: Path) -> None:
    imports_root = short_identity_root / "imports"
    research_root = short_identity_root / "research"
    run_root = short_identity_root / "runs"
    settings = _settings(
        short_identity_root,
        imports_root=imports_root,
        research_root=research_root,
        run_root=run_root,
    )
    imports = ResearchImportService(imports_root)
    research = build_local_research_service(settings)
    application = ResearchImportApplication(imports, research)
    first_pdf = _selectable_text_pdf("stable-content")
    second_pdf = _selectable_text_pdf("different-content")
    first_fingerprint = _fingerprint(first_pdf)
    second_fingerprint = _fingerprint(second_pdf)

    first = imports.create_received(
        user_id="alice",
        content=first_pdf,
        filename="original.pdf",
        source_revision="revision-original",
        source_fingerprint=first_fingerprint,
    )
    renamed = imports.create_received(
        user_id="alice",
        content=first_pdf,
        filename="renamed-copy.pdf",
        source_revision="revision-renamed",
        source_fingerprint=first_fingerprint,
    )
    different = imports.create_received(
        user_id="alice",
        content=second_pdf,
        filename="original.pdf",
        source_revision="revision-different",
        source_fingerprint=second_fingerprint,
    )
    other_owner = imports.create_received(
        user_id="bob",
        content=first_pdf,
        filename="original.pdf",
        source_revision="revision-bob",
        source_fingerprint=first_fingerprint,
    )

    first_result = application.process(user_id="alice", import_id=first["importId"])
    renamed_result = application.process(user_id="alice", import_id=renamed["importId"])
    different_result = application.process(user_id="alice", import_id=different["importId"])
    other_owner_result = application.process(user_id="bob", import_id=other_owner["importId"])

    assert first_result["status"] == renamed_result["status"] == "completed"
    assert first_result["paperId"] == renamed_result["paperId"]
    assert different_result["paperId"] != first_result["paperId"]
    assert other_owner_result["paperId"] != first_result["paperId"]

    # A restarted import/application/runtime stack must derive the same identity
    # from the durable owner scope and verified bytes, not from the new path.
    reopened_imports = ResearchImportService(imports_root)
    reopened_research = build_local_research_service(settings)
    reopened = ResearchImportApplication(reopened_imports, reopened_research)
    after_restart = reopened_imports.create_received(
        user_id="alice",
        content=first_pdf,
        filename="after-restart.pdf",
        source_revision="revision-restart",
        source_fingerprint=first_fingerprint,
    )
    restarted_result = reopened.process(user_id="alice", import_id=after_restart["importId"])
    assert restarted_result["paperId"] == first_result["paperId"]

    alice_scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    bob_scope = {"user_id": "bob", "memory_namespace": "research:user:bob"}
    store = FilesystemResearchCatalogStore(research_root / "paper-catalog")
    paper_id = first_result["paperId"]
    identity = store.get_identity(paper_id, actor_scope=alice_scope)
    document = store.get_document(paper_id, actor_scope=alice_scope)
    evidence = store.get_evidence(paper_id, actor_scope=alice_scope)
    catalog = store.get(paper_id, actor_scope=alice_scope)
    snapshots = store.list_for_paper(paper_id, actor_scope=alice_scope)

    assert identity is not None
    assert document is not None
    assert evidence is not None
    assert catalog is not None
    assert store.get_identity(paper_id, actor_scope=bob_scope) is None
    assert store.get_document(paper_id, actor_scope=bob_scope) is None
    assert {snapshot.metadata.get("source_revision") for snapshot in snapshots} >= {
        "revision-original",
        "revision-renamed",
        "revision-restart",
    }
    assert {snapshot.metadata.get("source_fingerprint") for snapshot in snapshots} == {
        first_fingerprint
    }
    assert all(snapshot.source_hash == first_fingerprint.removeprefix("sha256:") for snapshot in snapshots)
    assert imports.get(user_id="alice", import_id=first["importId"])["sourceRevision"] == "revision-original"
    assert reopened_imports.get(user_id="alice", import_id=after_restart["importId"])["paperId"] == paper_id


def test_real_local_import_concurrency_converges_on_one_paper_identity(short_identity_root: Path) -> None:
    imports_root = short_identity_root / "imports"
    settings = _settings(
        short_identity_root,
        imports_root=imports_root,
        research_root=short_identity_root / "research",
        run_root=short_identity_root / "runs",
    )
    imports = ResearchImportService(imports_root)
    research = build_local_research_service(settings)
    application = ResearchImportApplication(imports, research)
    pdf = _selectable_text_pdf("concurrent-content")
    fingerprint = _fingerprint(pdf)
    records = [
        imports.create_received(
            user_id="alice",
            content=pdf,
            filename=f"copy-{index}.pdf",
            source_revision=f"revision-{index}",
            source_fingerprint=fingerprint,
        )
        for index in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda record: application.process(
                    user_id="alice",
                    import_id=record["importId"],
                ),
                records,
            )
        )

    assert [result["status"] for result in results] == ["completed", "completed"]
    assert len({result["paperId"] for result in results}) == 1
    paper_id = results[0]["paperId"]
    scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    store = FilesystemResearchCatalogStore(settings.research_root / "paper-catalog")
    identity = store.get_identity(paper_id, actor_scope=scope)
    assert identity is not None
    assert len(identity.source_snapshot_refs) == 2
    assert len(store.list_for_paper(paper_id, actor_scope=scope)) == 2
    assert store.get_document(paper_id, actor_scope=scope) is not None
    assert store.get_evidence(paper_id, actor_scope=scope) is not None


@pytest.fixture
def short_identity_root() -> Path:
    root = (Path.cwd() / ".tmp" / "research-identity" / uuid4().hex).resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _settings(
    cwd: Path,
    *,
    imports_root: Path,
    research_root: Path,
    run_root: Path,
) -> ResearchLocalRuntimeSettings:
    return ResearchLocalRuntimeSettings.from_env(
        {
            "NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED": "0",
            "NEWS_RESEARCH_ROOT": str(research_root),
            "NEWSROOM_RESEARCH_IMPORT_DIR": str(imports_root),
            "NEWS_RESEARCH_RUN_STORE_ROOT": str(run_root),
        },
        cwd=cwd,
    )


def _fingerprint(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _selectable_text_pdf(marker: str) -> bytes:
    document = pymupdf.open()
    for index, heading in enumerate(("Introduction", "Method", "Results"), start=1):
        page = document.new_page()
        paragraph = (
            f"{heading}\n\n"
            f"{marker} section {index} validates deterministic local PDF identity. "
            "The document contains selectable text for the local parser and evidence builder. "
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

