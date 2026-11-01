"""One-time move of the single per-account key into a model connection.

Before connections existed every account stored one key for one catalog
provider and model in ``model-credentials.json``. On startup each such record
becomes a connection assigned to every purpose, re-sealed under the
connection binding. A key that no longer opens (the master key changed since)
is carried over as a connection that asks for its key again, so the account
keeps its provider and model choice.

The single-key file is renamed to ``.migrated`` only after the new document is
written. A crash in between leaves both files; the next start skips accounts
that already have connections, so the move never runs twice for anyone.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from framework.shared.windows_paths import storage_io_path
from interfaces.services.json_file_store import (
    JsonFileInvalidError,
    locked_json_file,
    read_json_object_unlocked,
)
from interfaces.services.model_connections.catalog import ModelProviderCatalog
from interfaces.services.model_connections.cipher import (
    CredentialCipher,
    SealedSecret,
    SecretKeyMismatchError,
    SecretTamperedError,
    connection_binding,
    single_key_credential_binding,
)
from interfaces.services.model_connections.errors import ModelConnectionConfigurationError
from interfaces.services.model_connections.purposes import ModelPurpose
from interfaces.services.model_connections.records import (
    AccountModelConnections,
    ModelConnectionRecord,
)
from interfaces.services.model_connections.store import LocalJsonModelConnectionRepository

UTC = timezone.utc
SINGLE_KEY_STORE_FILE_NAME = "model-credentials.json"
MIGRATED_SUFFIX = ".migrated"


def new_connection_id() -> str:
    return f"mc-{secrets.token_hex(8)}"


def migrate_single_key_credentials(
    *,
    single_key_path: str | Path,
    repository: LocalJsonModelConnectionRepository,
    cipher: CredentialCipher,
    catalog: ModelProviderCatalog,
    id_factory: Callable[[], str] = new_connection_id,
) -> int:
    """Move single-key records into ``repository``; return how many accounts moved."""

    single_key = Path(single_key_path)
    if not single_key.exists():
        return 0
    with locked_json_file(single_key) as single_key_resolved:
        try:
            payload = read_json_object_unlocked(single_key_resolved, default={}, strict=True)
        except JsonFileInvalidError as exc:
            raise ModelConnectionConfigurationError(f"{single_key} cannot be migrated: {exc}") from exc
        records = [_single_key_record(item, source=single_key) for item in payload.get("credentials") or ()]
        with locked_json_file(repository.path) as path:
            accounts = repository.read_unlocked(path)
            moved = 0
            for record in records:
                if accounts.get(record["user_id"]) is not None:
                    continue
                accounts[record["user_id"]] = _account(record, cipher=cipher, catalog=catalog, id_factory=id_factory)
                moved += 1
            if moved:
                repository.write_unlocked(path, accounts)
        storage_io_path(single_key_resolved).replace(
            storage_io_path(single_key_resolved.with_name(single_key_resolved.name + MIGRATED_SUFFIX))
        )
    return moved


def _account(
    record: dict[str, Any],
    *,
    cipher: CredentialCipher,
    catalog: ModelProviderCatalog,
    id_factory: Callable[[], str],
) -> AccountModelConnections:
    user_id = record["user_id"]
    provider_id = record["provider_id"]
    connection_id = id_factory()
    sealed: SealedSecret | None
    try:
        secret = cipher.open(record["sealed"], single_key_credential_binding(user_id, provider_id))
    except (SecretKeyMismatchError, SecretTamperedError):
        sealed = None
    else:
        sealed = cipher.seal(secret, connection_binding(user_id, connection_id))
    provider = catalog.provider(provider_id)
    label = f"{provider.label} · {record['model']}" if provider is not None else record["model"]
    connection = ModelConnectionRecord(
        connection_id=connection_id,
        provider_id=provider_id,
        label=label,
        model=record["model"],
        reasoning_effort=None,
        sealed=sealed,
        key_hint=record["key_hint"] if sealed is not None else None,
        revision=1,
        created_at=record["updated_at"],
        updated_at=record["updated_at"],
        verified_at=record["verified_at"] if sealed is not None else None,
    )
    return AccountModelConnections(
        user_id=user_id,
        revision=1,
        connections=(connection,),
        assignments={purpose: connection_id for purpose in ModelPurpose},
    )


def _single_key_record(raw: Any, *, source: Path) -> dict[str, Any]:
    try:
        verified = raw.get("verifiedAt")
        return {
            "user_id": str(raw["userId"]),
            "provider_id": str(raw["providerId"]),
            "model": str(raw["model"]),
            "sealed": SealedSecret(
                key_id=str(raw["keyId"]), nonce=str(raw["nonce"]), ciphertext=str(raw["ciphertext"])
            ),
            "key_hint": str(raw.get("keyHint") or "") or None,
            "updated_at": _parse(raw["updatedAt"]),
            "verified_at": _parse(verified) if verified else None,
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ModelConnectionConfigurationError(f"{source} holds a malformed credential record") from exc


def _parse(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


__all__ = [
    "SINGLE_KEY_STORE_FILE_NAME",
    "MIGRATED_SUFFIX",
    "migrate_single_key_credentials",
    "new_connection_id",
]
