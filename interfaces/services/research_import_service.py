from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from interfaces.services.json_file_store import locked_json_file, read_json_object_unlocked, write_json_object_unlocked

MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_IMPORTS_PER_USER = 20


class ResearchImportError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class ResearchImportService:
    """Owns uploaded bytes and lifecycle state; parsing stays in Research runtime."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("NEWSROOM_RESEARCH_IMPORT_DIR", ".newsroom/research/imports")).expanduser().resolve()
        self.index_path = self.root / "index.json"

    def create_received(self, *, user_id: str, content: bytes, filename: str | None = None) -> dict[str, Any]:
        if not content.startswith(b"%PDF"):
            raise ResearchImportError("invalid_pdf", "Only a valid PDF file can be imported")
        if len(content) > MAX_IMPORT_BYTES:
            raise ResearchImportError("pdf_too_large", "PDF exceeds the 50 MB limit", status_code=413)
        import_id = f"imp_{uuid4().hex}"
        path = self.root / user_id / f"{import_id}.pdf"
        record = {
            "importId": import_id, "userId": user_id, "status": "received",
            "filename": _safe_filename(filename), "sizeBytes": len(content),
            "createdAt": _now(), "updatedAt": _now(), "path": str(path),
        }
        self._write_record(record, content)
        return _public(record)

    def get(self, *, user_id: str, import_id: str) -> dict[str, Any]:
        return _public(self._owned(user_id, import_id))

    def mark_parsing(self, *, user_id: str, import_id: str) -> dict[str, Any]:
        record = self._owned(user_id, import_id)
        record.update(status="parsing", updatedAt=_now(), error=None)
        self._write_record(record)
        return _public(record)

    def mark_completed(self, *, user_id: str, import_id: str, result: dict[str, Any]) -> dict[str, Any]:
        record = self._owned(user_id, import_id)
        paper_id = _first_value(result, "paperId", "paper_id", "id") or _first_value(result.get("metadata"), "paperId", "paper_id", "id")
        record.update(status="completed", updatedAt=_now(), paperId=paper_id, result=_public_result(result), error=None)
        self._write_record(record)
        return _public(record)

    def mark_failed(self, *, user_id: str, import_id: str, code: str, message: str) -> dict[str, Any]:
        record = self._owned(user_id, import_id)
        record.update(status="failed", updatedAt=_now(), error={"code": code, "message": message})
        self._write_record(record)
        return _public(record)

    def source_path(self, *, user_id: str, import_id: str) -> Path:
        record = self._owned(user_id, import_id)
        path = Path(str(record.get("path", ""))).resolve()
        if self.root not in path.parents or path.suffix.casefold() != ".pdf" or not path.is_file():
            raise ResearchImportError("import_file_missing", "Imported PDF is unavailable", status_code=404)
        return path

    def _owned(self, user_id: str, import_id: str) -> dict[str, Any]:
        records = self._read_records()
        record = records.get(import_id)
        if not isinstance(record, dict) or record.get("userId") != user_id:
            raise ResearchImportError("import_not_found", "PDF import was not found", status_code=404)
        return dict(record)

    def _write_record(self, record: dict[str, Any], content: bytes | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with locked_json_file(self.index_path) as resolved:
            records = self._read_records_unlocked(resolved)
            if record["importId"] not in records:
                count = sum(1 for item in records.values() if item.get("userId") == record["userId"])
                if count >= MAX_IMPORTS_PER_USER:
                    raise ResearchImportError("import_limit_reached", "Too many PDF imports; remove an old import and retry", status_code=429)
            if content is not None:
                path = Path(str(record["path"]))
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(f".{uuid4().hex}.tmp")
                temp.write_bytes(content)
                temp.replace(path)
            records[str(record["importId"])] = record
            write_json_object_unlocked(resolved, {"imports": records})

    def _read_records(self) -> dict[str, dict[str, Any]]:
        with locked_json_file(self.index_path) as resolved:
            return self._read_records_unlocked(resolved)

    @staticmethod
    def _read_records_unlocked(path: Path) -> dict[str, dict[str, Any]]:
        raw = read_json_object_unlocked(path, default={"imports": {}}).get("imports", {})
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)} if isinstance(raw, dict) else {}


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"userId", "path", "result"}}


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {"paperId": _first_value(result, "paperId", "paper_id", "id") or _first_value(result.get("metadata"), "paperId", "paper_id", "id")}


def _first_value(value: Any, *keys: str) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return None


def _safe_filename(value: str | None) -> str:
    name = Path(str(value or "paper.pdf")).name.strip() or "paper.pdf"
    return name if name.casefold().endswith(".pdf") else f"{name}.pdf"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["MAX_IMPORT_BYTES", "ResearchImportError", "ResearchImportService"]
