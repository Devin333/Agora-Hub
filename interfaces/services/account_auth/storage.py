from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interfaces.services.json_file_store import locked_json_file, read_json_object_unlocked, write_json_object_unlocked

UTC = timezone.utc


class JsonTransactionStore:
    def __init__(self, path: str | Path, *, collection: str, schema_version: str) -> None:
        self.path = Path(path)
        self.collection = collection
        self.schema_version = schema_version

    def read(self) -> list[dict[str, Any]]:
        with locked_json_file(self.path) as path:
            return self._read(path)

    def mutate(self, operation: Callable[[list[dict[str, Any]]], Any]) -> Any:
        with locked_json_file(self.path) as path:
            records = self._read(path)
            result = operation(records)
            write_json_object_unlocked(
                path,
                {"schemaVersion": self.schema_version, self.collection: records},
            )
            return result

    def _read(self, path: Path) -> list[dict[str, Any]]:
        payload = read_json_object_unlocked(path, default={self.collection: []}, strict=True)
        records = payload.get(self.collection, [])
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise ValueError(f"{self.collection} must be a list of objects")
        return [dict(item) for item in records]


def keyed_digest(secret: str, purpose: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{purpose}\0{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def constant_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
