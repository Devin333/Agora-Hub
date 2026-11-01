"""Application service for each account's model connections.

Keys are write-only: sealed on save, opened only to serve a model call, a
verification or a model listing the user asked for, and never returned
through any view. A stored key is only ever sent to the provider and address
it was entered for; changing either requires entering the key again.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from framework.llm.clients import LLMCredentialUnavailableError, LLMEndpoint, LLMEndpointSource
from infrastructure.security.outbound_address import OutboundAddressPolicy
from interfaces.services.model_connections.catalog import CustomProvider, ModelProviderCatalog
from interfaces.services.model_connections.cipher import (
    CredentialCipher,
    SecretKeyMismatchError,
    SecretTamperedError,
    connection_binding,
)
from interfaces.services.model_connections.drafts import (
    ConnectionDraft,
    ValidatedDraft,
    normalized_key,
    validate_custom_address,
    validate_draft,
)
from interfaces.services.model_connections.errors import (
    ModelConnectionError,
    connection_required,
    invalid,
    key_missing,
    key_required,
    key_rotated,
    limit_reached,
    not_found,
    provider_unsupported,
    revision_conflict,
)
from interfaces.services.model_connections.migration import new_connection_id
from interfaces.services.model_connections.probe import ProbeClientFactory, discover_models, verify_endpoint
from interfaces.services.model_connections.purposes import ModelPurpose, PurposeRequirement
from interfaces.services.model_connections.records import AccountModelConnections, ModelConnectionRecord
from interfaces.services.model_connections.resolution import resolve_connection_spec
from interfaces.services.model_connections.store import ModelConnectionRepository
from interfaces.services.model_connections.views import account_view

UTC = timezone.utc
MAX_CONNECTIONS_PER_ACCOUNT = 16
# Verification only learns something about the key and the model, so only a
# change to one of them invalidates an earlier result.
_VERIFICATION_FIELDS = ("provider_id", "base_url", "model")
# Model ids discovery may be asked about need an address; this placeholder is
# never sent, a listing does not name a model.
_DISCOVERY_MODEL = "model-discovery"


class ModelConnectionService:
    def __init__(
        self,
        *,
        catalog: ModelProviderCatalog,
        cipher: CredentialCipher,
        repository: ModelConnectionRepository,
        requirements: Mapping[ModelPurpose, PurposeRequirement],
        probe_client_factory: ProbeClientFactory,
        outbound_policy: OutboundAddressPolicy,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] = new_connection_id,
        max_connections: int = MAX_CONNECTIONS_PER_ACCOUNT,
    ) -> None:
        missing = [purpose.value for purpose in ModelPurpose if purpose not in requirements]
        if missing:
            raise ValueError(f"every purpose needs a requirement; missing: {', '.join(missing)}")
        self.catalog = catalog
        self._cipher = cipher
        self._repository = repository
        self._requirements = dict(requirements)
        self._probe_client_factory = probe_client_factory
        self._outbound = outbound_policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory
        self._max_connections = max_connections

    @property
    def requirements(self) -> Mapping[ModelPurpose, PurposeRequirement]:
        return dict(self._requirements)

    # -- reading ---------------------------------------------------------

    def view(self, user_id: str) -> dict[str, Any]:
        return self._view(self._repository.get(user_id))

    # -- connections -----------------------------------------------------

    def create(self, user_id: str, draft: ConnectionDraft, *, expected_revision: int) -> dict[str, Any]:
        validated = validate_draft(draft, catalog=self.catalog, outbound=self._outbound)
        if validated.api_key is None:
            raise key_required()
        account = self._current(user_id, expected_revision)
        if len(account.connections) >= self._max_connections:
            raise limit_reached(self._max_connections)
        connection_id = self._id_factory()
        now = self._clock()
        record = ModelConnectionRecord(
            connection_id=connection_id,
            provider_id=validated.provider.id,
            label=validated.label,
            model=validated.model,
            reasoning_effort=validated.reasoning_effort,
            sealed=self._cipher.seal(validated.api_key, connection_binding(user_id, connection_id)),
            key_hint=validated.api_key[-4:],
            revision=1,
            created_at=now,
            updated_at=now,
            **_custom_fields(validated),
        )
        return self._write(account, account.with_connection(record))

    def update(
        self,
        user_id: str,
        connection_id: str,
        draft: ConnectionDraft,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Replace a connection's settings; an omitted key keeps the stored one.

        The stored key is kept only while it would go to the same provider
        and address; it is never forwarded to a new destination. A change
        that makes the connection unable to serve a purpose it is assigned to
        is refused, so an assignment never silently stops fitting.
        """

        validated = validate_draft(draft, catalog=self.catalog, outbound=self._outbound)
        account = self._current(user_id, expected_revision)
        current = account.connection(connection_id)
        if current is None:
            raise not_found()
        now = self._clock()
        if validated.api_key is None:
            if (current.provider_id, current.base_url) != validated.key_destination:
                raise key_required()
            if current.sealed is None:
                raise key_missing()
            if current.sealed.key_id != self._cipher.key_id:
                raise key_rotated()
            sealed, key_hint = current.sealed, current.key_hint
        else:
            sealed = self._cipher.seal(validated.api_key, connection_binding(user_id, connection_id))
            key_hint = validated.api_key[-4:]
        record = ModelConnectionRecord(
            connection_id=connection_id,
            provider_id=validated.provider.id,
            label=validated.label,
            model=validated.model,
            reasoning_effort=validated.reasoning_effort,
            sealed=sealed,
            key_hint=key_hint,
            revision=current.revision + 1,
            created_at=current.created_at,
            updated_at=now,
            **_custom_fields(validated),
        )
        keeps_verification = validated.api_key is None and all(
            getattr(record, name) == getattr(current, name) for name in _VERIFICATION_FIELDS
        )
        record = replace(record, verified_at=current.verified_at if keeps_verification else None)
        for purpose in account.purposes_of(connection_id):
            self._admit(purpose, record)
        return self._write(account, account.with_connection(record))

    def delete(self, user_id: str, connection_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Remove a connection and every assignment that pointed at it."""

        account = self._current(user_id, expected_revision)
        if account.connection(connection_id) is None:
            raise not_found()
        return self._write(account, account.without_connection(connection_id))

    def assign(
        self,
        user_id: str,
        assignments: Mapping[str, str | None],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Set which connection serves each purpose; ``None`` clears one.

        Purposes not named keep their assignment. Every new assignment must
        pass window and structured-output admission for its purpose.
        """

        account = self._current(user_id, expected_revision)
        merged: dict[ModelPurpose, str] = dict(account.assignments)
        for raw_purpose, connection_id in assignments.items():
            try:
                purpose = ModelPurpose.parse(raw_purpose)
            except ValueError:
                raise invalid("Unknown model purpose", field="assignments") from None
            if connection_id is None:
                merged.pop(purpose, None)
                continue
            record = account.connection(str(connection_id))
            if record is None:
                raise not_found()
            self._admit(purpose, record)
            merged[purpose] = record.connection_id
        return self._write(account, account.with_assignments(merged))

    # -- provider checks -------------------------------------------------

    def verify(self, user_id: str, connection_id: str) -> dict[str, Any]:
        """Check the stored key against the provider; one request, never retried.

        A key the provider rejects, or one that may not use the chosen
        model, clears an earlier verification.
        """

        account = self._repository.get(user_id)
        record = account.connection(connection_id)
        if record is None:
            raise not_found()
        spec = resolve_connection_spec(record, self.catalog)
        endpoint = spec.endpoint(api_key=self._open(user_id, record), connection_ref=record.ref)
        try:
            verify_endpoint(self._probe_client_factory, endpoint, custom=spec.custom)
        except ModelConnectionError as exc:
            if exc.code in {"model_connection_rejected", "model_connection_model_unavailable"}:
                self._record_verification(account, record, verified_at=None)
            raise
        return self._record_verification(account, record, verified_at=self._clock())

    def discover(
        self,
        user_id: str,
        *,
        provider_id: str,
        base_url: str | None = None,
        api_key: str | None = None,
        connection_id: str | None = None,
    ) -> tuple[str, ...]:
        """List the models a key may call at an address, without storing the key.

        Catalog providers answer from the catalog. For a custom provider the
        draft key is used, or the stored key of ``connection_id`` when the
        draft keeps that connection's provider and address.
        """

        provider = self.catalog.provider(str(provider_id or "").strip())
        if provider is None:
            raise provider_unsupported()
        if not isinstance(provider, CustomProvider):
            return tuple(model.id for model in provider.models)
        address = validate_custom_address(base_url, outbound=self._outbound)
        if api_key is not None:
            secret = normalized_key(api_key)
        elif connection_id is not None:
            record = self._repository.get(user_id).connection(connection_id)
            if record is None:
                raise not_found()
            if (record.provider_id, record.base_url) != (provider.id, address):
                raise key_required()
            secret = self._open(user_id, record)
        else:
            raise key_required()
        endpoint = LLMEndpoint(
            base_url=address,
            model=_DISCOVERY_MODEL,
            api_key=secret,
            provider=provider.id,
            protocol=provider.protocol,
        )
        return discover_models(self._probe_client_factory, endpoint)

    # -- serving model calls ---------------------------------------------

    def endpoint_for(self, user_id: str, purpose: ModelPurpose) -> LLMEndpoint:
        """Open the connection assigned to ``purpose`` for one call; raises a public error."""

        record = self._repository.get(user_id).assigned(purpose)
        if record is None:
            raise connection_required(purpose)
        spec = resolve_connection_spec(record, self.catalog)
        error = spec.admission_error(purpose, self._requirements[purpose])
        if error is not None:
            # The catalog changed since the assignment was admitted.
            raise error
        return spec.endpoint(api_key=self._open(user_id, record), connection_ref=record.ref)

    def endpoint_source(self, user_id: str) -> LLMEndpointSource:
        """The lazy per-purpose resolver bound for every model call of one request.

        Failures surface as the framework's ``LLMCredentialUnavailableError``
        so research code reports them without knowing about this service.
        """

        def resolve(raw_purpose: str) -> LLMEndpoint:
            try:
                purpose = ModelPurpose.parse(raw_purpose)
            except ValueError:
                raise LLMCredentialUnavailableError(
                    "model_connection_required", "Unknown model purpose", purpose=raw_purpose
                ) from None
            try:
                return self.endpoint_for(user_id, purpose)
            except ModelConnectionError as exc:
                raise LLMCredentialUnavailableError(exc.code, str(exc), purpose=purpose.value) from None

        return resolve

    # -- internals -------------------------------------------------------

    def _current(self, user_id: str, expected_revision: int) -> AccountModelConnections:
        account = self._repository.get(user_id)
        if account.revision != expected_revision:
            raise revision_conflict()
        return account

    def _write(self, before: AccountModelConnections, after: AccountModelConnections) -> dict[str, Any]:
        written = after.next_revision()
        if not self._repository.put(written, expected_revision=before.revision):
            raise revision_conflict()
        return self._view(written)

    def _admit(self, purpose: ModelPurpose, record: ModelConnectionRecord) -> None:
        error = resolve_connection_spec(record, self.catalog).admission_error(purpose, self._requirements[purpose])
        if error is not None:
            raise error

    def _open(self, user_id: str, record: ModelConnectionRecord) -> str:
        if record.sealed is None:
            raise key_missing()
        try:
            return self._cipher.open(record.sealed, connection_binding(user_id, record.connection_id))
        except SecretKeyMismatchError:
            raise key_rotated() from None
        except SecretTamperedError:
            raise key_missing() from None

    def _record_verification(
        self,
        account: AccountModelConnections,
        record: ModelConnectionRecord,
        *,
        verified_at: datetime | None,
    ) -> dict[str, Any]:
        # Verification does not change what a call sends, so the connection
        # revision (and the cached clients keyed by it) stays as it is.
        return self._write(account, account.with_connection(replace(record, verified_at=verified_at)))

    def _view(self, account: AccountModelConnections) -> dict[str, Any]:
        return account_view(
            account, catalog=self.catalog, cipher=self._cipher, requirements=self._requirements
        )


def _custom_fields(validated: ValidatedDraft) -> dict[str, Any]:
    return {
        "base_url": validated.base_url,
        "context_window_tokens": validated.context_window_tokens,
        "max_output_tokens": validated.max_output_tokens,
        "reasoning_parameter": validated.reasoning_parameter,
        "structured_output": validated.structured_output,
    }


__all__ = ["MAX_CONNECTIONS_PER_ACCOUNT", "ModelConnectionService"]
