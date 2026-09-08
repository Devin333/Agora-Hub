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
