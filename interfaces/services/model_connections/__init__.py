"""Per-account model connections and their assignment to model purposes."""

from interfaces.services.model_connections.catalog import (
    CatalogModel,
    CatalogProvider,
    CatalogReasoning,
    CatalogStructuredOutput,
    CustomProvider,
    ModelProviderCatalog,
    load_model_provider_catalog,
    parse_model_provider_catalog,
)
from interfaces.services.model_connections.cipher import (
    ENCRYPTION_KEY_ENV,
    CredentialCipher,
    generate_master_key,
)
from interfaces.services.model_connections.drafts import ConnectionDraft
from interfaces.services.model_connections.errors import (
    ModelConnectionConfigurationError,
    ModelConnectionError,
)
from interfaces.services.model_connections.migration import (
    SINGLE_KEY_STORE_FILE_NAME,
    migrate_single_key_credentials,
)
from interfaces.services.model_connections.probe import ProbeClientFactory
from interfaces.services.model_connections.purposes import ModelPurpose, PurposeRequirement, purpose_label
from interfaces.services.model_connections.service import ModelConnectionService
from interfaces.services.model_connections.store import LocalJsonModelConnectionRepository
from interfaces.services.model_connections.views import ModelConnectionStatus

__all__ = [
    "CatalogModel",
    "CatalogProvider",
    "CatalogReasoning",
    "CatalogStructuredOutput",
    "ConnectionDraft",
    "CredentialCipher",
    "CustomProvider",
    "ENCRYPTION_KEY_ENV",
    "SINGLE_KEY_STORE_FILE_NAME",
    "LocalJsonModelConnectionRepository",
    "ModelConnectionConfigurationError",
    "ModelConnectionError",
    "ModelConnectionService",
    "ModelConnectionStatus",
    "ModelProviderCatalog",
    "ModelPurpose",
    "ProbeClientFactory",
    "PurposeRequirement",
    "generate_master_key",
    "load_model_provider_catalog",
    "migrate_single_key_credentials",
    "parse_model_provider_catalog",
    "purpose_label",
]
