from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import unquote

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

    def create_received(
        self,
        *,
        user_id: str,
        content: bytes,
        filename: str | None = None,
        import_id: str | None = None,
        source_revision: str | None = None,
        source_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if not content.startswith(b"%PDF"):
            raise ResearchImportError("invalid_pdf", "Only a valid PDF file can be imported")
        if len(content) > MAX_IMPORT_BYTES:
            raise ResearchImportError("pdf_too_large", "PDF exceeds the 50 MB limit", status_code=413)
        actual_import_id = _import_id(import_id) if import_id is not None else f"imp_{uuid4().hex}"
        path = self.root / user_id / f"{actual_import_id}.pdf"
        record = {
            "importId": actual_import_id, "userId": user_id, "status": "received",
            "filename": _safe_filename(filename), "sizeBytes": len(content),
            "createdAt": _now(), "updatedAt": _now(), "path": str(path),
            **({"sourceRevision": _required_identity(source_revision, "sourceRevision")} if source_revision is not None else {}),
            **({"sourceFingerprint": _source_fingerprint(source_fingerprint)} if source_fingerprint is not None else {}),
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
        if not paper_id:
            raise ResearchImportError("import_document_missing", "转换未生成可阅读的论文，请重试。", status_code=502, retryable=True)
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
        path, _content = self._verified_source(record, user_id=user_id)
        return path

    def read_source(self, *, user_id: str, import_id: str) -> bytes:
        record = self._owned(user_id, import_id)
        _path, content = self._verified_source(record, user_id=user_id)
        return content

    def _verified_source(
        self,
        record: dict[str, Any],
        *,
        user_id: str,
    ) -> tuple[Path, bytes]:
        raw_path = Path(str(record.get("path", ""))).expanduser()
        if raw_path.is_symlink():
            raise ResearchImportError(
                "import_file_invalid", "Imported PDF identity is invalid", status_code=409
            )
        path = raw_path.resolve()
        owner_root = (self.root / user_id).resolve()
        if owner_root not in path.parents or path.suffix.casefold() != ".pdf" or not path.is_file():
            raise ResearchImportError(
                "import_file_missing", "Imported PDF is unavailable", status_code=404
            )
        content = path.read_bytes()
        expected_size = int(record.get("sizeBytes") or -1)
        if len(content) > MAX_IMPORT_BYTES or len(content) != expected_size:
            raise ResearchImportError(
                "import_file_corrupt",
                "Imported PDF content no longer matches its recorded identity",
                status_code=409,
            )
        fingerprint = record.get("sourceFingerprint")
        if fingerprint is not None:
            digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if digest != _source_fingerprint(str(fingerprint)):
                raise ResearchImportError(
                    "import_file_corrupt",
                    "Imported PDF content no longer matches its recorded identity",
                    status_code=409,
                )
        return path, content

    def resolve_completed_source(
        self,
        *,
        user_id: str,
        paper_id: str,
        source_revision: str,
        source_fingerprint: str,
    ) -> dict[str, Any]:
        """Resolve an exact, owner-scoped imported source without exposing its file locator."""

        expected_revision = _required_identity(source_revision, "sourceRevision")
        expected_fingerprint = _source_fingerprint(source_fingerprint)
        matches = [
            record
            for record in self._read_records().values()
            if record.get("userId") == user_id
            and record.get("status") == "completed"
            and str(record.get("paperId") or "") == paper_id
            and _source_revision_matches(record.get("sourceRevision"), expected_revision, expected_fingerprint)
            and record.get("sourceFingerprint") == expected_fingerprint
        ]
        if not matches:
            raise ResearchImportError(
                "import_source_not_found",
                "The exact imported paper source is unavailable for this account",
                status_code=404,
            )
        record = max(matches, key=lambda item: str(item.get("updatedAt") or ""))
        path = self.source_path(user_id=user_id, import_id=str(record["importId"]))
        return {
            "importId": str(record["importId"]),
            "paperId": paper_id,
            "sourceRevision": expected_revision,
            "sourceFingerprint": expected_fingerprint,
            "filename": str(record.get("filename") or "paper.pdf"),
            "sizeBytes": int(record.get("sizeBytes") or path.stat().st_size),
            "path": path,
        }

    @contextmanager
    def processing(self, *, user_id: str, import_id: str):
        """Only one parser may own an import, including across API processes.

        A process exit releases the OS lock, so an interrupted parsing record is
        recoverable without allowing concurrent retries to execute twice.
        """
        source = self.source_path(user_id=user_id, import_id=import_id)
        with locked_json_file(source.with_suffix(".processing")):
            yield self.get(user_id=user_id, import_id=import_id)

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


def _required_identity(value: str | None, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 512 or any(ord(char) < 0x20 for char in text):
        raise ResearchImportError("invalid_import_identity", f"{field} is invalid")
    return text


def _source_revision_matches(stored: Any, expected: str, fingerprint: str) -> bool:
    def normalized(value: Any) -> str:
        text = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", text):
            return f"sha256:{text}"
        return text

    expected_identity = normalized(expected)
    stored_identity = normalized(stored)
    return stored_identity == expected_identity or (stored_identity == fingerprint and expected_identity == fingerprint)


def _source_fingerprint(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ResearchImportError("invalid_import_identity", "sourceFingerprint must be sha256:<hex>")
    return text


def _import_id(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"imp_[0-9a-f]{32,64}", text):
        raise ResearchImportError("invalid_import_id", "Import identity is invalid")
    return text


def _safe_filename(value: str | None) -> str:
    name = Path(unquote(str(value or "paper.pdf")).replace("\\", "/")).name.strip() or "paper.pdf"
    return name if name.casefold().endswith(".pdf") else f"{name}.pdf"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["MAX_IMPORT_BYTES", "ResearchImportError", "ResearchImportService"]
