"""Stored shape of an account's model connections (ciphertext only)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from framework.llm.models.reasoning import ReasoningEffort, ReasoningWire
from interfaces.services.model_connections.cipher import SealedSecret
from interfaces.services.model_connections.purposes import ModelPurpose

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class ModelConnectionRecord:
    """One connection: which provider and model, its settings and its sealed key.

    Fields that only custom providers take (``base_url``, ``context_window_tokens``,
    ``max_output_tokens``, ``reasoning_parameter``, ``structured_output``) are
    ``None`` for catalog providers, whose values come from the catalog when the
    connection is resolved, so an operator correction applies to every account.
    """

    connection_id: str
    provider_id: str
    label: str
    model: str
    # ``None`` sends nothing and leaves the provider's default in force.
    reasoning_effort: ReasoningEffort | None
    # ``None`` when the key must be entered again (a migrated key that no
    # longer opened under the current master key).
    sealed: SealedSecret | None
    key_hint: str | None
    revision: int
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None
    base_url: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    reasoning_parameter: ReasoningWire | None = None
    structured_output: str | None = None

    @property
    def ref(self) -> str:
        """Non-secret identity of this exact version of the connection."""
        return f"{self.connection_id}@{self.revision}"

    def to_record(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "connectionId": self.connection_id,
            "providerId": self.provider_id,
            "label": self.label,
            "model": self.model,
            "reasoningEffort": self.reasoning_effort.value if self.reasoning_effort else None,
            "keyHint": self.key_hint,
            "revision": self.revision,
            "createdAt": _format(self.created_at),
            "updatedAt": _format(self.updated_at),
            "verifiedAt": _format(self.verified_at) if self.verified_at else None,
            "baseUrl": self.base_url,
            "contextWindowTokens": self.context_window_tokens,
            "maxOutputTokens": self.max_output_tokens,
            "reasoningParameter": self.reasoning_parameter.value if self.reasoning_parameter else None,
            "structuredOutput": self.structured_output,
            "sealed": (
                {
                    "keyId": self.sealed.key_id,
                    "nonce": self.sealed.nonce,
                    "ciphertext": self.sealed.ciphertext,
                }
                if self.sealed is not None
                else None
            ),
        }
        return payload

    @classmethod
    def from_record(cls, payload: Mapping[str, Any]) -> ModelConnectionRecord:
        sealed = payload.get("sealed")
        effort = payload.get("reasoningEffort")
        parameter = payload.get("reasoningParameter")
        return cls(
            connection_id=str(payload["connectionId"]),
            provider_id=str(payload["providerId"]),
            label=str(payload["label"]),
            model=str(payload["model"]),
            reasoning_effort=ReasoningEffort.parse(effort) if effort else None,
            sealed=(
                SealedSecret(
                    key_id=str(sealed["keyId"]),
                    nonce=str(sealed["nonce"]),
                    ciphertext=str(sealed["ciphertext"]),
                )
                if isinstance(sealed, Mapping)
                else None
            ),
            key_hint=str(payload["keyHint"]) if payload.get("keyHint") else None,
            revision=int(payload["revision"]),
            created_at=_parse(payload["createdAt"]),
            updated_at=_parse(payload["updatedAt"]),
            verified_at=_parse(payload["verifiedAt"]) if payload.get("verifiedAt") else None,
            base_url=str(payload["baseUrl"]) if payload.get("baseUrl") else None,
            context_window_tokens=_optional_int(payload.get("contextWindowTokens")),
            max_output_tokens=_optional_int(payload.get("maxOutputTokens")),
            reasoning_parameter=ReasoningWire.parse(parameter) if parameter else None,
            structured_output=str(payload["structuredOutput"]) if payload.get("structuredOutput") else None,
        )


@dataclass(frozen=True, slots=True)
class AccountModelConnections:
    """Everything one account stored: its connections and which serves each purpose.

    ``revision`` changes with every write, so it is the one compare-and-set
    token for all of the account's model settings.
    """

    user_id: str
    revision: int = 0
    connections: tuple[ModelConnectionRecord, ...] = ()
    assignments: Mapping[ModelPurpose, str] = field(default_factory=dict)

    @classmethod
    def empty(cls, user_id: str) -> AccountModelConnections:
        return cls(user_id=user_id)

    def connection(self, connection_id: str) -> ModelConnectionRecord | None:
        return next((item for item in self.connections if item.connection_id == connection_id), None)

    def assigned(self, purpose: ModelPurpose) -> ModelConnectionRecord | None:
        connection_id = self.assignments.get(purpose)
        return self.connection(connection_id) if connection_id is not None else None

    def purposes_of(self, connection_id: str) -> tuple[ModelPurpose, ...]:
        return tuple(purpose for purpose in ModelPurpose if self.assignments.get(purpose) == connection_id)

    def with_connection(self, record: ModelConnectionRecord) -> AccountModelConnections:
        if self.connection(record.connection_id) is None:
            return replace(self, connections=(*self.connections, record))
        # Keep the list order stable so the settings page does not reshuffle.
        return replace(
            self,
            connections=tuple(record if item.connection_id == record.connection_id else item for item in self.connections),
        )

    def without_connection(self, connection_id: str) -> AccountModelConnections:
        return replace(
            self,
            connections=tuple(item for item in self.connections if item.connection_id != connection_id),
            assignments={
                purpose: assigned for purpose, assigned in self.assignments.items() if assigned != connection_id
            },
        )

    def with_assignments(self, assignments: Mapping[ModelPurpose, str]) -> AccountModelConnections:
        return replace(self, assignments=dict(assignments))

    def next_revision(self) -> AccountModelConnections:
        return replace(self, revision=self.revision + 1)

    def to_record(self) -> dict[str, Any]:
        return {
            "userId": self.user_id,
            "revision": self.revision,
            "connections": [item.to_record() for item in self.connections],
            "assignments": {
                purpose.value: self.assignments[purpose] for purpose in ModelPurpose if purpose in self.assignments
            },
        }

    @classmethod
    def from_record(cls, payload: Mapping[str, Any]) -> AccountModelConnections:
        connections = tuple(ModelConnectionRecord.from_record(item) for item in payload.get("connections") or ())
        known = {item.connection_id for item in connections}
        raw_assignments = payload.get("assignments") or {}
        assignments: dict[ModelPurpose, str] = {}
        for raw_purpose, connection_id in raw_assignments.items():
            try:
                purpose = ModelPurpose.parse(raw_purpose)
            except ValueError:
                # A purpose a newer release wrote; this one has no use for it.
                continue
            if str(connection_id) in known:
                assignments[purpose] = str(connection_id)
        return cls(
            user_id=str(payload["userId"]),
            revision=int(payload["revision"]),
            connections=connections,
            assignments=assignments,
        )


def format_timestamp(value: datetime) -> str:
    return _format(value)


def _format(value: datetime) -> str:
    actual = value if value.tzinfo else value.replace(tzinfo=UTC)
    return actual.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


__all__ = ["AccountModelConnections", "ModelConnectionRecord", "format_timestamp"]
