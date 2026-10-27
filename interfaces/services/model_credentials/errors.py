from __future__ import annotations


class ModelCredentialError(RuntimeError):
    """A model-credential request failed; ``code`` is the public error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class ModelCredentialConfigurationError(RuntimeError):
    """The operator configuration (catalog or master key) is unusable."""


def provider_unsupported() -> ModelCredentialError:
    return ModelCredentialError(
        "model_provider_unsupported",
        "The selected model provider or model is not offered by this service",
        status_code=400,
    )


def credential_required() -> ModelCredentialError:
    return ModelCredentialError(
        "model_credential_required",
        "Add your model API key in settings before running this step",
        status_code=409,
    )


def credential_key_rotated() -> ModelCredentialError:
    return ModelCredentialError(
        "model_credential_key_rotated",
        "The service encryption key changed; enter your model API key again",
        status_code=409,
    )


def credential_rejected() -> ModelCredentialError:
    return ModelCredentialError(
        "model_credential_rejected",
        "The model provider rejected this API key",
        status_code=400,
    )


def provider_unreachable() -> ModelCredentialError:
    return ModelCredentialError(
        "model_provider_unreachable",
        "The model provider could not be reached; try verifying again later",
        status_code=502,
        retryable=True,
    )


def revision_conflict() -> ModelCredentialError:
    return ModelCredentialError(
        "model_credential_conflict",
        "Model settings changed in another window; reload and try again",
        status_code=409,
    )
