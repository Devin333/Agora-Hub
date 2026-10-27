"""Application service for each account's own model API key.

Keys are write-only: they are sealed on save, opened only to serve a model
call or an explicit verification, and never returned through any view.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from framework.llm import LLMRequest
from framework.llm.clients import (
    LLMCredentialMode,
    LLMCredentialUnavailableError,
    LLMEndpoint,
    LLMEndpointSource,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    bind_llm_endpoint_source,
)
from framework.llm.clients.openai_compatible import (
    LLMProviderError,
    LLMRetryPolicy,
    Transport,
)
from interfaces.services.model_credentials.catalog import ModelProviderCatalog
from interfaces.services.model_credentials.cipher import (
    CredentialCipher,
    CredentialKeyMismatchError,
    CredentialTamperedError,
)
from interfaces.services.model_credentials.errors import (
    ModelCredentialError,
    credential_key_rotated,
    credential_rejected,
    credential_required,
    provider_unreachable,
    revision_conflict,
)
from interfaces.services.model_credentials.store import (
    ModelCredentialRecord,
    ModelCredentialRepository,
)

UTC = timezone.utc
VERIFY_TIMEOUT_SECONDS = 20.0
_REJECTED_STATUS_CODES = frozenset({401, 403})
_MAX_KEY_LENGTH = 512


class ModelCredentialStatus:
    NOT_CONFIGURED = "not_configured"
    KEY_ROTATED = "key_rotated"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class ModelCredentialView:
    """The only shape in which a stored credential ever leaves the service."""

    status: str
    provider_id: str | None = None
    model: str | None = None
    key_hint: str | None = None
    revision: int | None = None
    updated_at: datetime | None = None
    verified_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return self.status in {ModelCredentialStatus.UNVERIFIED, ModelCredentialStatus.VERIFIED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "status": self.status,
            "providerId": self.provider_id,
            "model": self.model,
            "keyHint": self.key_hint,
            "revision": self.revision,
            "updatedAt": _format(self.updated_at),
            "verifiedAt": _format(self.verified_at),
        }


class ModelCredentialService:
    def __init__(
        self,
        *,
        catalog: ModelProviderCatalog,
        cipher: CredentialCipher,
        repository: ModelCredentialRepository,
        verification_transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.catalog = catalog
        self._cipher = cipher
        self._repository = repository
        self._verification_transport = verification_transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def view(self, user_id: str) -> ModelCredentialView:
        record = self._repository.get(user_id)
        if record is None:
            return ModelCredentialView(status=ModelCredentialStatus.NOT_CONFIGURED)
        return _view(record, key_current=record.sealed.key_id == self._cipher.key_id)

    def save(
        self,
        user_id: str,
        *,
        provider_id: str,
        model: str,
        api_key: str | None,
        expected_revision: int | None,
    ) -> ModelCredentialView:
        """Store a key, or change only the model when ``api_key`` is omitted.

        A new key or provider clears the verification result; switching the
        model of the same provider keeps the key but also asks for a new
        verification, since a key may be scoped to some models only.
        """

        provider, catalog_model = self.catalog.require(provider_id, model)
        current = self._repository.get(user_id)
        current_revision = current.revision if current is not None else None
        if current_revision != expected_revision:
            raise revision_conflict()
        now = self._clock()
        if api_key is None:
            if current is None or current.provider_id != provider.id:
                raise credential_required()
            if current.sealed.key_id != self._cipher.key_id:
                raise credential_key_rotated()
            record = replace(
                current,
                model=catalog_model.id,
                revision=current.revision + 1,
                updated_at=now,
                verified_at=current.verified_at if current.model == catalog_model.id else None,
            )
        else:
            secret = _normalized_key(api_key)
            record = ModelCredentialRecord(
                user_id=user_id,
                provider_id=provider.id,
                model=catalog_model.id,
                sealed=self._cipher.seal(secret, user_id=user_id, provider_id=provider.id),
                key_hint=secret[-4:],
                revision=(current_revision or 0) + 1,
                updated_at=now,
            )
        if not self._repository.put(record, expected_revision=expected_revision):
            raise revision_conflict()
        return _view(record, key_current=True)

    def delete(self, user_id: str) -> ModelCredentialView:
        self._repository.delete(user_id)
        return ModelCredentialView(status=ModelCredentialStatus.NOT_CONFIGURED)

    def endpoint_for(self, user_id: str) -> LLMEndpoint:
        """Open the account's key for one model call; raises a public error."""

        record = self._repository.get(user_id)
        if record is None:
            raise credential_required()
        return self._endpoint(record)

    def endpoint_source(self, user_id: str) -> LLMEndpointSource:
        """Return the lazy resolver bound for every model call of one request.

        Failures surface as the framework's ``LLMCredentialUnavailableError``
        so research code reports them without knowing about this service.
        """

        def resolve() -> LLMEndpoint:
            try:
                return self.endpoint_for(user_id)
            except ModelCredentialError as exc:
                raise LLMCredentialUnavailableError(exc.code, str(exc)) from None

        return resolve

    def verify(self, user_id: str) -> ModelCredentialView:
        """Send exactly one minimal request with the stored key.

        No retry is made: a verification costs the user a provider call, and
        a second attempt would hide whether the first one was billed.
        """

        record = self._repository.get(user_id)
        if record is None:
            raise credential_required()
        endpoint = self._endpoint(record)
        client = OpenAICompatibleClient(
            OpenAICompatibleConfig(
                provider=record.provider_id,
                base_url=endpoint.base_url,
                model=endpoint.model,
                # Never read: invocation mode takes the key from the binding.
                api_key_env="NEWSROOM_UNUSED_INVOCATION_KEY",
                timeout_seconds=VERIFY_TIMEOUT_SECONDS,
            ),
            transport=self._verification_transport,
            retry_policy=LLMRetryPolicy(max_attempts=1),
            credential_mode=LLMCredentialMode.INVOCATION,
        )
        request = LLMRequest(
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            max_transport_attempts=1,
        )
        try:
            with bind_llm_endpoint_source(lambda: endpoint):
                client.complete(request)
        except LLMProviderError as exc:
            if exc.status_code in _REJECTED_STATUS_CODES:
                self._record_verification(record, verified_at=None)
                raise credential_rejected() from None
            raise provider_unreachable() from None
        except OSError:
            raise provider_unreachable() from None
        verified = self._record_verification(record, verified_at=self._clock())
        return _view(verified, key_current=True)

    def _endpoint(self, record: ModelCredentialRecord) -> LLMEndpoint:
        try:
            provider, model = self.catalog.require(record.provider_id, record.model)
        except ModelCredentialError:
            # The operator withdrew this provider or model after it was saved.
            raise credential_required() from None
        try:
            secret = self._cipher.open(
                record.sealed, user_id=record.user_id, provider_id=record.provider_id
            )
        except CredentialKeyMismatchError:
            raise credential_key_rotated() from None
        except CredentialTamperedError:
            raise credential_required() from None
        return LLMEndpoint(base_url=provider.base_url, model=model.id, api_key=secret)

    def _record_verification(
        self, record: ModelCredentialRecord, *, verified_at: datetime | None
    ) -> ModelCredentialRecord:
        updated = replace(record, verified_at=verified_at, revision=record.revision + 1)
        if not self._repository.put(updated, expected_revision=record.revision):
            # The user saved something else meanwhile; that newer record wins.
            raise revision_conflict()
        return updated


def _normalized_key(api_key: str) -> str:
    secret = str(api_key or "").strip()
    if (
        len(secret) < 8
        or len(secret) > _MAX_KEY_LENGTH
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in secret)
    ):
        raise ModelCredentialError(
            "model_credential_invalid",
            "The API key must be 8-512 visible ASCII characters",
            status_code=400,
        )
    return secret


def _view(record: ModelCredentialRecord, *, key_current: bool) -> ModelCredentialView:
    if not key_current:
        status = ModelCredentialStatus.KEY_ROTATED
    elif record.verified_at is not None:
        status = ModelCredentialStatus.VERIFIED
    else:
        status = ModelCredentialStatus.UNVERIFIED
    return ModelCredentialView(
        status=status,
        provider_id=record.provider_id,
        model=record.model,
        key_hint=record.key_hint if key_current else None,
        revision=record.revision,
        updated_at=record.updated_at,
        verified_at=record.verified_at if key_current else None,
    )


def _format(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ModelCredentialService",
    "ModelCredentialStatus",
    "ModelCredentialView",
    "VERIFY_TIMEOUT_SECONDS",
]
