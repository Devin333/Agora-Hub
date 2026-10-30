"""The only shapes in which stored connections leave the service.

No view carries a key: a connection reports whether a key is configured and
its last four characters, nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from framework.llm.models.reasoning import ReasoningEffort, reasoning_output_allowance
from interfaces.services.model_connections.catalog import ModelProviderCatalog
from interfaces.services.model_connections.cipher import CredentialCipher
from interfaces.services.model_connections.errors import ModelConnectionError
from interfaces.services.model_connections.purposes import ModelPurpose, PurposeRequirement
from interfaces.services.model_connections.records import (
    AccountModelConnections,
    ModelConnectionRecord,
    format_timestamp,
)
from interfaces.services.model_connections.resolution import ConnectionSpec, resolve_connection_spec


class ModelConnectionStatus:
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    # A migrated key that no longer opened: the key must be entered again.
    KEY_MISSING = "key_missing"
    # Sealed under a master key the service no longer holds.
    KEY_ROTATED = "key_rotated"
    # The operator no longer offers its provider or model.
    WITHDRAWN = "withdrawn"


def connection_status(record: ModelConnectionRecord, *, cipher: CredentialCipher, withdrawn: bool) -> str:
    if withdrawn:
        return ModelConnectionStatus.WITHDRAWN
    if record.sealed is None:
        return ModelConnectionStatus.KEY_MISSING
    if record.sealed.key_id != cipher.key_id:
        return ModelConnectionStatus.KEY_ROTATED
    if record.verified_at is not None:
        return ModelConnectionStatus.VERIFIED
    return ModelConnectionStatus.UNVERIFIED


def account_view(
    account: AccountModelConnections,
    *,
    catalog: ModelProviderCatalog,
    cipher: CredentialCipher,
    requirements: Mapping[ModelPurpose, PurposeRequirement],
) -> dict[str, Any]:
    return {
        "revision": account.revision,
        "connections": [
            _connection_view(record, account=account, catalog=catalog, cipher=cipher, requirements=requirements)
            for record in account.connections
        ],
        "assignments": {purpose.value: account.assignments.get(purpose) for purpose in ModelPurpose},
        "purposes": [
            {"id": purpose.value, "label": purpose.label, "requirement": requirements[purpose].to_dict()}
            for purpose in ModelPurpose
        ],
        # Output each tier reserves for reasoning, so a form can show whether
        # a draft fits a purpose before it is saved; "default" is the
        # provider's own tier, used when no effort is chosen.
        "reasoningOutputAllowance": {
            "default": reasoning_output_allowance(None),
            **{effort.value: reasoning_output_allowance(effort) for effort in ReasoningEffort},
        },
        "catalog": catalog.to_dict(),
    }


def _connection_view(
    record: ModelConnectionRecord,
    *,
    account: AccountModelConnections,
    catalog: ModelProviderCatalog,
    cipher: CredentialCipher,
    requirements: Mapping[ModelPurpose, PurposeRequirement],
) -> dict[str, Any]:
    spec: ConnectionSpec | None
    try:
        spec = resolve_connection_spec(record, catalog)
    except ModelConnectionError:
        spec = None
    status = connection_status(record, cipher=cipher, withdrawn=spec is None)
    key_usable = status in {ModelConnectionStatus.VERIFIED, ModelConnectionStatus.UNVERIFIED}
    return {
        "id": record.connection_id,
        "label": record.label,
        "providerId": record.provider_id,
        "providerLabel": spec.provider_label if spec is not None else record.provider_id,
        "custom": spec.custom if spec is not None else record.base_url is not None,
        "protocol": spec.protocol.value if spec is not None else None,
        "baseUrl": spec.base_url if spec is not None else record.base_url,
        "model": record.model,
        "modelLabel": spec.model_label if spec is not None else record.model,
        "reasoningEffort": (
            spec.reasoning.effort.value
            if spec is not None and spec.reasoning.effort is not None
            else None
        ),
        "reasoningParameter": spec.reasoning.wire.value if spec is not None else None,
        "contextWindowTokens": spec.context_window_tokens if spec is not None else record.context_window_tokens,
        "maxOutputTokens": spec.max_output_tokens if spec is not None else record.max_output_tokens,
        "structuredOutput": spec.structured_output.mode if spec is not None else record.structured_output,
        "status": status,
        "credentialConfigured": key_usable,
        "keyHint": record.key_hint if key_usable else None,
        "revision": record.revision,
        "createdAt": format_timestamp(record.created_at),
        "updatedAt": format_timestamp(record.updated_at),
        "verifiedAt": format_timestamp(record.verified_at) if record.verified_at and key_usable else None,
        "purposes": [purpose.value for purpose in account.purposes_of(record.connection_id)],
        "fits": {
            purpose.value: _fit(spec, purpose, requirements[purpose]) for purpose in ModelPurpose
        },
    }


def _fit(spec: ConnectionSpec | None, purpose: ModelPurpose, requirement: PurposeRequirement) -> dict[str, Any]:
    if spec is None:
        return {"fits": False, "requiredWindow": None, "reason": "model_connection_withdrawn"}
    error = spec.admission_error(purpose, requirement)
    return {
        "fits": error is None,
        "requiredWindow": spec.required_window(requirement),
        "reason": error.code if error is not None else None,
    }


__all__ = ["ModelConnectionStatus", "account_view", "connection_status"]
