"""Public errors of the model-connection service.

``code`` is the stable value the API and the browser switch on; messages are
for logs and API clients, the browser shows its own Chinese text per code.
``details`` carries only non-secret facts the UI needs to explain the error
(which purpose, how large a window it needs).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from interfaces.services.model_connections.purposes import ModelPurpose


class ModelConnectionError(RuntimeError):
    """A model-connection request failed; ``code`` is the public error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.details = dict(details or {})


class ModelConnectionConfigurationError(RuntimeError):
    """The operator configuration (catalog, master key, single-key store) is unusable."""


def _purpose_details(purpose: ModelPurpose, **extra: Any) -> dict[str, Any]:
    return {"purpose": purpose.value, "purposeLabel": purpose.label, **extra}


def invalid(message: str, *, field: str) -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_invalid", message, status_code=400, details={"field": field}
    )


def provider_unsupported() -> ModelConnectionError:
    return ModelConnectionError(
        "model_provider_unsupported",
        "The selected model provider or model is not offered by this service",
        status_code=400,
    )


def not_found() -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_not_found", "This model connection does not exist", status_code=404
    )


def limit_reached(limit: int) -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_limit_reached",
        f"An account may keep at most {limit} model connections",
        status_code=400,
        details={"limit": limit},
    )


def key_required() -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_key_required",
        "Enter the API key: a new connection, provider or address needs one",
        status_code=400,
    )


def key_rotated() -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_key_rotated",
        "The service encryption key changed; enter this connection's API key again",
        status_code=409,
    )


def connection_required(purpose: ModelPurpose) -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_required",
        f"Assign a model connection to {purpose.value} in settings before running this step",
        status_code=409,
        details=_purpose_details(purpose),
    )


def key_missing() -> ModelConnectionError:
    # A migrated key that no longer opened, or a record that failed to
    # authenticate: the connection is kept, the key must be entered again.
    return ModelConnectionError(
        "model_connection_key_missing",
        "This connection's API key must be entered again",
        status_code=409,
    )


def connection_withdrawn() -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_withdrawn",
        "The operator no longer offers this connection's provider or model",
        status_code=409,
    )


def window_too_small(
    purpose: ModelPurpose, *, required_tokens: int, context_window_tokens: int
) -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_window_too_small",
        f"{purpose.value} needs a context window of at least {required_tokens} tokens",
        status_code=400,
        # Key names avoid "token": API error details pass through the secret
        # redactor, which hides any key containing it.
        details=_purpose_details(
            purpose,
            requiredWindow=required_tokens,
            contextWindow=context_window_tokens,
        ),
    )


def structured_output_required(purpose: ModelPurpose) -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_structured_output_required",
        f"{purpose.value} needs a model that returns JSON objects",
        status_code=400,
        details=_purpose_details(purpose),
    )


def credential_rejected() -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_rejected", "The model provider rejected this API key", status_code=400
    )


def model_unavailable() -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_model_unavailable",
        "The model provider accepted this API key but does not offer it the selected model",
        status_code=400,
    )


def discovery_unsupported() -> ModelConnectionError:
    return ModelConnectionError(
        "model_discovery_unsupported",
        "This address does not list its models; enter the model id by hand",
        status_code=400,
    )


def provider_unreachable() -> ModelConnectionError:
    return ModelConnectionError(
        "model_provider_unreachable",
        "The model provider could not be reached; try again later",
        status_code=502,
        retryable=True,
    )


def endpoint_not_allowed() -> ModelConnectionError:
    return ModelConnectionError(
        "model_endpoint_not_allowed",
        "This model address is not allowed: it must be public HTTPS, or an administrator must allowlist it",
        status_code=400,
    )


def revision_conflict() -> ModelConnectionError:
    return ModelConnectionError(
        "model_connection_conflict",
        "Model settings changed in another window; reload and try again",
        status_code=409,
    )


__all__ = [
    "ModelConnectionConfigurationError",
    "ModelConnectionError",
    "connection_required",
    "connection_withdrawn",
    "credential_rejected",
    "discovery_unsupported",
    "endpoint_not_allowed",
    "invalid",
    "key_missing",
    "key_required",
    "key_rotated",
    "limit_reached",
    "model_unavailable",
    "not_found",
    "provider_unreachable",
    "provider_unsupported",
    "revision_conflict",
    "structured_output_required",
    "window_too_small",
]
