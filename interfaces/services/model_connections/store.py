"""Durable per-account model connections: one JSON document on the data volume."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from interfaces.services.json_file_store import (
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)
from interfaces.services.model_connections.records import AccountModelConnections

STORE_SCHEMA_VERSION = "agora_model_connections.v1"
STORE_FILE_NAME = "model-connections.json"


class ModelConnectionRepository(Protocol):
    def get(self, user_id: str) -> AccountModelConnections: ...
    def put(self, account: AccountModelConnections, *, expected_revision: int) -> bool: ...


class LocalJsonModelConnectionRepository:
    """All accounts in one document, written under the shared file lock.

    ``put`` is a compare-and-set on the account revision, so two browser tabs
    cannot silently overwrite each other's connections or assignments.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def under(cls, account_data_root: str | Path) -> LocalJsonModelConnectionRepository:
        return cls(Path(account_data_root) / STORE_FILE_NAME)

    def get(self, user_id: str) -> AccountModelConnections:
        with locked_json_file(self.path) as path:
            return self.read_unlocked(path).get(user_id) or AccountModelConnections.empty(user_id)

    def put(self, account: AccountModelConnections, *, expected_revision: int) -> bool:
        with locked_json_file(self.path) as path:
            accounts = self.read_unlocked(path)
            current = accounts.get(account.user_id)
            if (current.revision if current is not None else 0) != expected_revision:
                return False
            accounts[account.user_id] = account
            self.write_unlocked(path, accounts)
            return True

    def read_unlocked(self, path: Path) -> dict[str, AccountModelConnections]:
        payload = read_json_object_unlocked(
            path, default={"schemaVersion": STORE_SCHEMA_VERSION, "accounts": []}, strict=True
        )
        accounts = (AccountModelConnections.from_record(item) for item in payload.get("accounts") or ())
        return {account.user_id: account for account in accounts}

    def write_unlocked(self, path: Path, accounts: dict[str, AccountModelConnections]) -> None:
        write_json_object_unlocked(
            path,
            {
                "schemaVersion": STORE_SCHEMA_VERSION,
                "accounts": [accounts[user_id].to_record() for user_id in sorted(accounts)],
            },
        )


__all__ = [
    "LocalJsonModelConnectionRepository",
    "ModelConnectionRepository",
    "STORE_FILE_NAME",
    "STORE_SCHEMA_VERSION",
]
