"""Composition root for the hosted (multi-account, server-side) profile.

Everything the hosted boundary needs is assembled and validated here once, at
startup: a missing master key, a non-invocation credential mode or a provider
catalog that cannot hold one research request stops the process instead of
surfacing later as a confusing request failure.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from framework.llm.clients import CREDENTIAL_MODE_ENV, LLMCredentialMode, llm_credential_mode
from interfaces.services.account_administration import AccountAdministrationService
from interfaces.services.auth_service import AuthApplicationService
from interfaces.services.deployment_profile import (
    DeploymentConfigurationError,
    HostedSettings,
    load_hosted_settings,
)
from interfaces.services.model_credentials import (
    ENCRYPTION_KEY_ENV,
    CredentialCipher,
    LocalJsonModelCredentialRepository,
    ModelCredentialConfigurationError,
    ModelCredentialService,
    ModelProviderCatalog,
    load_model_provider_catalog,
)

PROVIDER_CATALOG_ENV = "NEWSROOM_HOSTED_LLM_PROVIDERS_PATH"
DEFAULT_PROVIDER_CATALOG = "configs/hosted-llm-providers.yaml"
USERS_FILE_NAME = "users.json"
SESSIONS_FILE_NAME = "sessions.json"


@dataclass(frozen=True, slots=True)
class HostedAccountRuntime:
    """The account store alone, as the server CLI needs it."""

    settings: HostedSettings
    auth: AuthApplicationService
    accounts: AccountAdministrationService


@dataclass(frozen=True, slots=True)
class HostedRuntime:
    settings: HostedSettings
    auth: AuthApplicationService
    accounts: AccountAdministrationService
    model_credentials: ModelCredentialService


def build_hosted_auth_service(settings: HostedSettings) -> AuthApplicationService:
    """Account store shared by the API and the server CLI."""

    root = settings.account_data_root
    return AuthApplicationService(
        user_store_path=root / USERS_FILE_NAME,
        session_store_path=root / SESSIONS_FILE_NAME,
    )


def build_hosted_account_runtime(env: Mapping[str, str] | None = None) -> HostedAccountRuntime:
    """Account administration without the model-credential master key.

    The operator managing accounts on the server does not need to hold the
    credential encryption key, so it is not required here.
    """

    settings = load_hosted_settings(env if env is not None else os.environ)
    auth = build_hosted_auth_service(settings)
    return HostedAccountRuntime(
        settings=settings, auth=auth, accounts=AccountAdministrationService(auth)
    )


def build_hosted_runtime(
    env: Mapping[str, str] | None = None,
    *,
    research_request_tokens: int | None = None,
) -> HostedRuntime:
    values = env if env is not None else os.environ
    settings = load_hosted_settings(values)
    if llm_credential_mode(values) is not LLMCredentialMode.INVOCATION:
        raise DeploymentConfigurationError(
            CREDENTIAL_MODE_ENV, "must be invocation in the hosted profile"
        )
    _reject_process_key_features(values)
    try:
        cipher = CredentialCipher.from_env(values)
    except ModelCredentialConfigurationError as exc:
        raise DeploymentConfigurationError(ENCRYPTION_KEY_ENV, str(exc)) from exc
    catalog = _catalog(values)
    tokens = (
        research_request_tokens
        if research_request_tokens is not None
        else _research_request_tokens(values)
    )
    try:
        catalog.require_context_window(tokens)
    except ModelCredentialConfigurationError as exc:
        raise DeploymentConfigurationError(PROVIDER_CATALOG_ENV, str(exc)) from exc
    auth = build_hosted_auth_service(settings)
    return HostedRuntime(
        settings=settings,
        auth=auth,
        accounts=AccountAdministrationService(auth),
        model_credentials=ModelCredentialService(
            catalog=catalog,
            cipher=cipher,
            repository=LocalJsonModelCredentialRepository.under(settings.account_data_root),
        ),
    )


# Features that call a model with the process-wide key instead of the
# signed-in account's key. Enabled in hosted, they would bill the operator for
# every account and bypass the per-account credential entirely.
PROCESS_KEY_FEATURE_FLAGS = ("NEWS_VISUAL_DESCRIPTION_ENABLED",)


def _reject_process_key_features(values: Mapping[str, str]) -> None:
    for name in PROCESS_KEY_FEATURE_FLAGS:
        if str(values.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}:
            raise DeploymentConfigurationError(
                name, "uses the process model key and must stay off in the hosted profile"
            )


def _catalog(values: Mapping[str, str]) -> ModelProviderCatalog:
    path = Path(str(values.get(PROVIDER_CATALOG_ENV) or DEFAULT_PROVIDER_CATALOG).strip())
    try:
        return load_model_provider_catalog(path)
    except ModelCredentialConfigurationError as exc:
        raise DeploymentConfigurationError(PROVIDER_CATALOG_ENV, str(exc)) from exc


def _research_request_tokens(values: Mapping[str, str]) -> int:
    # Imported here: research settings pull in the research runtime package,
    # which the account CLI does not need.
    from interfaces.composition.research_errors import ResearchCompositionError
    from interfaces.composition.research_settings import ResearchRuntimeSettings

    try:
        llm = ResearchRuntimeSettings.from_env(values).llm
    except ResearchCompositionError as exc:
        raise DeploymentConfigurationError(
            "NEWS_RESEARCH_LLM_*", f"research model settings are invalid: {exc}"
        ) from exc
    return llm.max_input_tokens + llm.max_output_tokens


__all__ = [
    "DEFAULT_PROVIDER_CATALOG",
    "HostedAccountRuntime",
    "HostedRuntime",
    "PROVIDER_CATALOG_ENV",
    "build_hosted_account_runtime",
    "build_hosted_auth_service",
    "build_hosted_runtime",
]
