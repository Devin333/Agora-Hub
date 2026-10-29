from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone as _tz
from pathlib import Path

from framework.workers.approval import (
    ApprovalDecision,
    ApprovalNotFoundError,
    ApprovalRequest,
    ApprovalStatus,
)
from infrastructure.storage.local_json.file_lock import LocalJsonFileLock


APPROVAL_STORE_SCHEMA_VERSION = "approval_store.v1"


class LocalJsonApprovalStore:
    def __init__(
        self,
        path: str | Path = ".newsroom/approvals/approvals.json",
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.now_fn = now_fn or (lambda: datetime.now(_tz.utc))
        self._lock = LocalJsonFileLock(self.path)

    def list_approvals(self, *, status: ApprovalStatus | str | None = None) -> list[ApprovalRequest]:
        records = sorted(self._read_records().values(), key=lambda request: request.created_at)
        if status is None:
            return records
        actual_status = ApprovalStatus(status)
        return [request for request in records if request.status == actual_status]

    def get_approval(self, approval_id: str) -> ApprovalRequest:
        return _require(self._read_records(), approval_id)

    def upsert_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._lock.hold():
            records = self._read_records()
            records[request.approval_id] = request
            self._write_records(records)
        return request

    def record_decision(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
    ) -> ApprovalRequest:
        with self._lock.hold():
            records = self._read_records()
            request = _require(records, approval_id)
            decided = request.with_decision(decision)
            records[approval_id] = decided
            self._write_records(records)
        return decided

    def consume_approval(
        self,
        approval_id: str,
        *,
        consumed_by: str,
        consumed_at: datetime | None = None,
    ) -> ApprovalRequest:
        with self._lock.hold():
            records = self._read_records()
            request = _require(records, approval_id)
            consumed = request.with_consumption(
                consumed_by=consumed_by,
                consumed_at=consumed_at or self.now_fn(),
            )
            records[approval_id] = consumed
            self._write_records(records)
        return consumed

    def _read_records(self) -> dict[str, ApprovalRequest]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        approvals = payload.get("approvals", [])
        records = [ApprovalRequest.from_dict(item) for item in approvals]
        return {record.approval_id: record for record in records}

    def _write_records(self, records: dict[str, ApprovalRequest]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": APPROVAL_STORE_SCHEMA_VERSION,
            "approvals": [
                record.to_dict()
                for record in sorted(records.values(), key=lambda item: item.created_at)
            ],
        }
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self.path)


def _require(records: dict[str, ApprovalRequest], approval_id: str) -> ApprovalRequest:
    try:
        return records[approval_id]
    except KeyError as exc:
        raise ApprovalNotFoundError(approval_id) from exc
