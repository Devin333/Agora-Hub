"""Durable per-account model credential records (ciphertext only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from interfaces.services.json_file_store import (
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)
from interfaces.services.model_credentials.cipher import SealedSecret

UTC = timezone.utc
STORE_SCHEMA_VERSION = "agora_model_credentials.v1"
STORE_FILE_NAME = "model-credentials.json"


@dataclass(frozen=True, slots=True)
class ModelCredentialRecord:
    user_id: str
    provider_id: str
    model: str
    sealed: SealedSecret
    key_hint: str
    revision: int
    updated_at: datetime
    verified_at: datetime | None = None

    def to_record(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "userId": self.user_id,
            "providerId": self.provider_id,
            "model": self.model,
            "keyId": self.sealed.key_id,
            "nonce": self.sealed.nonce,
            "ciphertext": self.sealed.ciphertext,
            "keyHint": self.key_hint,
            "revision": self.revision,
            "updatedAt": _format(self.updated_at),
        }
        if self.verified_at is not None:
            payload["verifiedAt"] = _format(self.verified_at)
        return payload

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> "ModelCredentialRecord":
        verified = payload.get("verifiedAt")
        return cls(
            user_id=str(payload["userId"]),
            provider_id=str(payload["providerId"]),
            model=str(payload["model"]),
            sealed=SealedSecret(
                key_id=str(payload["keyId"]),
                nonce=str(payload["nonce"]),
                ciphertext=str(payload["ciphertext"]),
            ),
            key_hint=str(payload.get("keyHint") or ""),
            revision=int(payload["revision"]),
            updated_at=_parse(payload["updatedAt"]),
            verified_at=_parse(verified) if verified else None,
        )


class ModelCredentialRepository(Protocol):
    def get(self, user_id: str) -> ModelCredentialRecord | None: ...
    def put(self, record: ModelCredentialRecord, *, expected_revision: int | None) -> bool: ...
    def delete(self, user_id: str) -> bool: ...


class LocalJsonModelCredentialRepository:
    """One JSON document keyed by user id, written under the shared file lock.

    ``put`` is a compare-and-set on ``revision`` so two browser tabs cannot
    silently overwrite each other's key or verification result.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def under(cls, account_data_root: str | Path) -> "LocalJsonModelCredentialRepository":
        return cls(Path(account_data_root) / STORE_FILE_NAME)

    def get(self, user_id: str) -> ModelCredentialRecord | None:
        with locked_json_file(self.path) as path:
            return self._read_unlocked(path).get(user_id)

    def put(self, record: ModelCredentialRecord, *, expected_revision: int | None) -> bool:
        with locked_json_file(self.path) as path:
            records = self._read_unlocked(path)
            current = records.get(record.user_id)
            current_revision = current.revision if current is not None else None
            if current_revision != expected_revision:
                return False
            records[record.user_id] = record
            self._write_unlocked(path, records)
            return True

    def delete(self, user_id: str) -> bool:
        with locked_json_file(self.path) as path:
            records = self._read_unlocked(path)
            if records.pop(user_id, None) is None:
                return False
            self._write_unlocked(path, records)
            return True

    def _read_unlocked(self, path: Path) -> dict[str, ModelCredentialRecord]:
        payload = read_json_object_unlocked(
            path, default={"schemaVersion": STORE_SCHEMA_VERSION, "credentials": []}, strict=True
        )
        return {
            record.user_id: record
            for record in (
                ModelCredentialRecord.from_record(item) for item in payload.get("credentials", [])
            )
        }

    def _write_unlocked(self, path: Path, records: dict[str, ModelCredentialRecord]) -> None:
        write_json_object_unlocked(
            path,
            {
                "schemaVersion": STORE_SCHEMA_VERSION,
                "credentials": [
                    records[user_id].to_record() for user_id in sorted(records)
                ],
            },
        )


def _format(value: datetime) -> str:
    actual = value if value.tzinfo else value.replace(tzinfo=UTC)
    return actual.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


__all__ = [
    "LocalJsonModelCredentialRepository",
    "ModelCredentialRecord",
    "ModelCredentialRepository",
    "STORE_FILE_NAME",
]
