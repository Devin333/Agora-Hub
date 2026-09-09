from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from interfaces.services.research_import_application import ResearchImportApplication
from interfaces.services.research_import_service import ResearchImportError, ResearchImportService


def _research(parse):
    return SimpleNamespace(parse_paper=parse, get_document=lambda paper_id, **kwargs: {"paperId": paper_id, "document": {"status": "parsed", "sections": [{"text": "Parsed body"}]}})


def test_received_identity_survives_restart_and_concurrent_retry_parses_once(tmp_path):
    store = ResearchImportService(tmp_path)
    record = store.create_received(user_id="alice", content=b"%PDF-1.7\nbody", filename="%E7%A0%94%E7%A9%B6.pdf")
    assert record["filename"] == "研究.pdf"
    entered, release = Event(), Event()
    calls = []

    def parse(command):
        from backend.research.application.parse_paper import ParsePaperRequest

        ParsePaperRequest(source=command.source, source_type=command.source_type, options=command.options)
        calls.append(command)
        entered.set()
        assert release.wait(5)
        return {"paperId": "parsed-paper", "qualityReport": {"passed": True}}

    app = ResearchImportApplication(store, _research(parse))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(app.process, user_id="alice", import_id=record["importId"])
        assert entered.wait(5)
        reopened = ResearchImportService(tmp_path)
        assert reopened.get(user_id="alice", import_id=record["importId"])["status"] == "parsing"
        second = pool.submit(ResearchImportApplication(reopened, _research(parse)).process, user_id="alice", import_id=record["importId"])
        release.set()
        assert first.result()["paperId"] == second.result()["paperId"] == "parsed-paper"
    assert len(calls) == 1
    assert calls[0].user_id == "alice"
    assert calls[0].source.startswith("file:///")
    with pytest.raises(ResearchImportError):
        app.process(user_id="bob", import_id=record["importId"])


def test_interrupted_or_failed_conversion_can_resume_without_reupload(tmp_path):
    store = ResearchImportService(tmp_path)
    record = store.create_received(user_id="alice", content=b"%PDF-1.7\nbody")
    store.mark_parsing(user_id="alice", import_id=record["importId"])
    empty = ResearchImportApplication(store, _research(lambda command: {}))
    assert empty.process(user_id="alice", import_id=record["importId"])["status"] == "failed"
    valid = ResearchImportApplication(store, _research(lambda command: {"paperId": "paper", "qualityReport": {"passed": True}}))
    assert valid.process(user_id="alice", import_id=record["importId"])["status"] == "completed"


def test_degraded_parser_output_never_becomes_a_readable_import(tmp_path):
    store = ResearchImportService(tmp_path)
    record = store.create_received(user_id="alice", content=b"%PDF-1.7\nbody")
    app = ResearchImportApplication(store, _research(lambda command: {"paperId": "paper", "qualityReport": {"passed": False}}))
    result = app.process(user_id="alice", import_id=record["importId"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "import_quality_failed"
    assert "扫描件暂不支持" in result["error"]["message"]
    assert "paperId" not in result
