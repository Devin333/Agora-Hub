import hashlib
from pathlib import Path

import pytest

from interfaces.services.research_import_service import ResearchImportError, ResearchImportService


def test_pdf_import_is_durable_owner_scoped_and_hides_private_path(tmp_path: Path) -> None:
    service = ResearchImportService(tmp_path / "imports")
    record = service.create_received(user_id="alice", content=b"%PDF-1.7\nbody", filename="paper")
    assert record["status"] == "received"
    assert "path" not in record and "userId" not in record
    assert service.source_path(user_id="alice", import_id=record["importId"]).read_bytes().startswith(b"%PDF")
    with pytest.raises(ResearchImportError, match="not found"):
        service.get(user_id="bob", import_id=record["importId"])
    reopened = ResearchImportService(tmp_path / "imports")
    assert reopened.get(user_id="alice", import_id=record["importId"])["filename"] == "paper.pdf"


def test_import_rejects_non_pdf_and_bound_is_enforced(tmp_path: Path) -> None:
    service = ResearchImportService(tmp_path / "imports")
    with pytest.raises(ResearchImportError, match="valid PDF"):
        service.create_received(user_id="alice", content=b"not-pdf")
    for index in range(20):
        service.create_received(user_id="alice", content=b"%PDF" + bytes([index]), filename=f"{index}.pdf")
    with pytest.raises(ResearchImportError, match="Too many"):
        service.create_received(user_id="alice", content=b"%PDF-extra")


def test_received_import_rejects_a_fingerprint_that_does_not_match_the_bytes(tmp_path: Path) -> None:
    service = ResearchImportService(tmp_path / "imports")

    with pytest.raises(ResearchImportError) as mismatch:
        service.create_received(
            user_id="alice",
            content=b"%PDF-1.7\nbody",
            source_fingerprint="sha256:" + "0" * 64,
        )

    assert mismatch.value.code == "source_fingerprint_mismatch"
    assert mismatch.value.status_code == 409


def test_completed_import_resolves_only_exact_owner_and_source_identity(tmp_path: Path) -> None:
    service = ResearchImportService(tmp_path / "imports")
    content = b"%PDF-1.7\nbody"
    fingerprint = f"sha256:{hashlib.sha256(content).hexdigest()}"
    record = service.create_received(
        user_id="alice",
        content=content,
        filename="%E8%AE%BA%E6%96%87%2520literal.pdf",
        source_revision=fingerprint,
        source_fingerprint=fingerprint,
    )
    service.mark_completed(
        user_id="alice",
        import_id=record["importId"],
        result={"paperId": "2401.01234"},
    )

    source = service.resolve_completed_source(
        user_id="alice", paper_id="2401.01234",
        source_revision=fingerprint.removeprefix("sha256:"), source_fingerprint=fingerprint,
    )
    assert source["path"].read_bytes().startswith(b"%PDF")
    assert source["filename"] == "论文%20literal.pdf"
    with pytest.raises(ResearchImportError) as other_owner:
        service.resolve_completed_source(
            user_id="bob", paper_id="2401.01234",
            source_revision=fingerprint, source_fingerprint=fingerprint,
        )
    assert other_owner.value.code == "import_source_not_found"
    with pytest.raises(ResearchImportError):
        service.resolve_completed_source(
            user_id="alice", paper_id="2401.01234",
            source_revision=fingerprint, source_fingerprint="sha256:" + "b" * 64,
        )


def test_completed_import_rejects_tampered_pdf_bytes(tmp_path: Path) -> None:
    service = ResearchImportService(tmp_path / "imports")
    content = b"%PDF-1.7\noriginal"
    fingerprint = f"sha256:{hashlib.sha256(content).hexdigest()}"
    record = service.create_received(
        user_id="alice",
        content=content,
        source_revision=fingerprint,
        source_fingerprint=fingerprint,
    )
    service.mark_completed(
        user_id="alice",
        import_id=record["importId"],
        result={"paperId": "paper-private"},
    )
    path = service.source_path(user_id="alice", import_id=record["importId"])
    path.write_bytes(b"%PDF-1.7\ntampered")

    with pytest.raises(ResearchImportError) as corrupted:
        service.resolve_completed_source(
            user_id="alice",
            paper_id="paper-private",
            source_revision=fingerprint,
            source_fingerprint=fingerprint,
        )
    assert corrupted.value.code == "import_file_corrupt"
