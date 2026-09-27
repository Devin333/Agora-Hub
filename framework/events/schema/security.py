from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from framework.events.errors import (
    EventReservedFieldError,
    EventSecurePayloadRequiredError,
    EventSecurityError,
)
from framework.events.schema.policy import (
    FieldDisposition,
    SensitivityPolicy,
    WholeDocumentReferenceDisposition,
)


class SecurityClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


PROTECTED_CLASSIFICATIONS = {
    SecurityClassification.CONFIDENTIAL,
    SecurityClassification.RESTRICTED,
}

RESERVED_EVENT_FIELDS = frozenset(
    {
        "envelope_schema",
        "event_id",
        "event_type",
        "data_schema",
        "source",
        "subject",
        "occurred_at",
        "observed_at",
        "stream_id",
        "stream_sequence",
        "correlation_id",
        "causation_id",
        "business_context",
        "producer",
        "trace",
        "tenant_id",
        "security_classification",
        "content_type",
        "payload",
        "payload_ref",
        "extensions",
        "content_checksum",
        "record_checksum",
    }
)

# Retired flat orchestration fields remain reserved so no extension can revive
# them as a new authority channel.
_LEGACY_AUTHORITY_FIELDS = frozenset(
    {
        "agent_id",
        "classification",
        "component",
        "created_at",
        "event",
        "event_envelope",
        "is_remote",
        "parent_span_id",
        "request_id",
        "run_id",
        "schema_version",
        "sequence",
        "span_id",
        "step_id",
        "task_id",
        "tenant",
        "timestamp",
        "tool_call_id",
        "trace_flags",
        "trace_id",
        "tracestate",
        "workflow_id",
    }
)
_RESERVED_AUTHORITY_FIELDS = RESERVED_EVENT_FIELDS | _LEGACY_AUTHORITY_FIELDS
_AUTHORITY_CONTAINER_KEYS = frozenset({"metadata", "envelope"})
_LEGACY_EXTENSION_NAMESPACE = "io.newsroom.legacy"
_CANONICAL_PAYLOAD_REFERENCE_FIELDS = frozenset(
    {"uri", "expected_checksum", "content_type", "size_bytes"}
)

# Exact normalized key matches only. Legitimate keys such as ``token_count``
# and ``secretary`` must not be redacted by substring coincidence.
DEFAULT_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "credentials",
        "database_url",
        "dsn",
        "id_token",
        "jwt",
        "passphrase",
        "password",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "set_cookie",
        "ssh_private_key",
        "access_token",
        "x_api_key",
    }
)


def redact_event_value(
    value: Any,
    *,
    forbidden_secret_keys: frozenset[str] = DEFAULT_FORBIDDEN_SECRET_KEYS,
) -> Any:
    """Return a detached export-safe value using exact secret-key policy.

    Secret values found under forbidden keys are also removed from sibling
    diagnostic strings so compatibility exports cannot leak the same token in
    a free-form message. This helper is for read/export projection only; live
    publication still uses the fail-closed projector above.
    """

    normalized_keys = frozenset(_normalize_key(key) for key in forbidden_secret_keys)
    secret_values: set[str] = set()
    _collect_secret_values(value, normalized_keys, secret_values)
    return _redact_value(value, normalized_keys, secret_values)


class SecurePayloadCapability(StrEnum):
    TENANT_AUTHORIZATION = "tenant_authorization"
    ENCRYPTION_IN_TRANSIT = "encryption_in_transit"
    ENCRYPTION_AT_REST = "encryption_at_rest"
    INTEGRITY_VERIFICATION = "integrity_verification"
    AUDITED_ACCESS = "audited_access"


REQUIRED_SECURE_PAYLOAD_CAPABILITIES = frozenset(SecurePayloadCapability)


@dataclass(frozen=True)
class _CanonicalPayloadReferenceIdentity:
    uri: str
    expected_checksum: str
    content_type: str
    size_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "expected_checksum": self.expected_checksum,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class SecurePayloadValidation:
    """Secure-store evidence bound to one canonical ref and authority scope."""

    uri: str
    expected_checksum: str
    content_type: str
    size_bytes: int | None
    tenant_id: str
    classification: SecurityClassification | str
    capabilities: frozenset[SecurePayloadCapability | str]

    def __post_init__(self) -> None:
        identity = _canonical_payload_reference_identity(
            {
                "uri": self.uri,
                "expected_checksum": self.expected_checksum,
                "content_type": self.content_type,
                "size_bytes": self.size_bytes,
            }
        )
        tenant_id = _optional_text(self.tenant_id)
        if tenant_id is None:
            raise ValueError("secure payload validation tenant_id is required")
        try:
            classification = SecurityClassification(self.classification)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "secure payload validation classification is invalid"
            ) from exc
        if isinstance(self.capabilities, (str, bytes, bytearray)):
            raise TypeError("secure payload validation capabilities must be a collection")
        try:
            capabilities = frozenset(
                SecurePayloadCapability(capability) for capability in self.capabilities
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid secure payload capability") from exc
        object.__setattr__(self, "uri", identity.uri)
        object.__setattr__(self, "expected_checksum", identity.expected_checksum)
        object.__setattr__(self, "content_type", identity.content_type)
        object.__setattr__(self, "size_bytes", identity.size_bytes)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "classification", classification)
        object.__setattr__(self, "capabilities", capabilities)

    @classmethod
    def for_reference(
        cls,
        reference: Mapping[str, Any],
        *,
        tenant_id: str,
        classification: SecurityClassification | str,
        capabilities: frozenset[SecurePayloadCapability | str] = (
            REQUIRED_SECURE_PAYLOAD_CAPABILITIES
        ),
    ) -> SecurePayloadValidation:
        identity = _canonical_payload_reference_identity(reference)
        return cls(
            uri=identity.uri,
            expected_checksum=identity.expected_checksum,
            content_type=identity.content_type,
            size_bytes=identity.size_bytes,
            tenant_id=tenant_id,
            classification=classification,
            capabilities=capabilities,
        )

    @property
    def complete(self) -> bool:
        return self.capabilities == REQUIRED_SECURE_PAYLOAD_CAPABILITIES

    def proves(
        self,
        reference: Mapping[str, Any],
        *,
        tenant_id: str,
        classification: SecurityClassification,
    ) -> bool:
        identity = _canonical_payload_reference_identity(reference)
        return (
            self.uri == identity.uri
            and self.expected_checksum == identity.expected_checksum
            and self.content_type == identity.content_type
            and self.size_bytes == identity.size_bytes
            and self.tenant_id == tenant_id
            and self.classification is classification
            and self.complete
        )


@runtime_checkable
class SecurePayloadStorePort(Protocol):
    def validate_reference(
        self,
        reference: Mapping[str, Any],
        *,
        tenant_id: str,
        classification: SecurityClassification,
    ) -> SecurePayloadValidation:
        ...


@dataclass(frozen=True)
class SecurityProjection:
    payload: Mapping[str, Any] | None
    payload_ref: Mapping[str, Any] | None
    extensions: Mapping[str, Any]
    tenant_id: str | None
    classification: SecurityClassification

    def __post_init__(self) -> None:
        if self.payload is not None and not isinstance(self.payload, Mapping):
            raise EventSecurityError("projected payload must be an object")
        if self.payload_ref is not None and not isinstance(self.payload_ref, Mapping):
            raise EventSecurityError("projected payload reference must be an object")
        if not isinstance(self.extensions, Mapping):
            raise EventSecurityError("projected extensions must be an object")
        object.__setattr__(
            self,
            "payload",
            None if self.payload is None else _freeze_projected_mapping(self.payload),
        )
        object.__setattr__(
            self,
            "payload_ref",
            (
                None
                if self.payload_ref is None
                else _freeze_projected_mapping(self.payload_ref)
            ),
        )
        object.__setattr__(
            self,
            "extensions",
            _freeze_projected_mapping(self.extensions),
        )
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id))
        object.__setattr__(
            self,
            "classification",
            SecurityClassification(self.classification),
        )


@dataclass(frozen=True)
class SecurityExportProjection:
    """Detached schema-aware fields safe for compatibility exports."""

    payload: Mapping[str, Any] | None
    extensions: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.payload is not None and not isinstance(self.payload, Mapping):
            raise EventSecurityError("export payload must be an object")
        if not isinstance(self.extensions, Mapping):
            raise EventSecurityError("export extensions must be an object")
        object.__setattr__(
            self,
            "payload",
            None if self.payload is None else _freeze_projected_mapping(self.payload),
        )
        object.__setattr__(
            self,
            "extensions",
            _freeze_projected_mapping(self.extensions),
        )


class EventSecurityProjector:
    """Applies one fail-closed schema/security policy before durable writes."""

    def __init__(
        self,
        *,
        secure_payload_store: SecurePayloadStorePort | None = None,
        forbidden_secret_keys: frozenset[str] = DEFAULT_FORBIDDEN_SECRET_KEYS,
    ) -> None:
        self._secure_payload_store = secure_payload_store
        self._forbidden_secret_keys = frozenset(
            _normalize_key(key) for key in forbidden_secret_keys
        )

    def project(
        self,
        *,
        payload: Mapping[str, Any] | None,
        payload_ref: Any | None,
        extensions: Mapping[str, Any] | None,
        policy: SensitivityPolicy,
        classification: SecurityClassification | str = SecurityClassification.INTERNAL,
        tenant_id: str | None = None,
    ) -> SecurityProjection:
        classification_invalid = False
        try:
            actual_classification = SecurityClassification(classification)
        except (TypeError, ValueError):
            classification_invalid = True
            actual_classification = SecurityClassification.INTERNAL
        if classification_invalid:
            raise EventSecurityError("invalid security classification")
        actual_tenant = _optional_text(tenant_id)
        if extensions is not None and not isinstance(extensions, Mapping):
            raise EventSecurityError("event extensions must be an object")
        actual_extensions = _thaw_projector_value(extensions or {}, path="/extensions")
        self._validate_reserved_extensions(actual_extensions)
        policy_secret_values: set[str] = set()
        _collect_policy_secret_values(
            actual_extensions,
            policy=policy,
            pointer="/extensions",
            collected=policy_secret_values,
        )

        if payload is not None and payload_ref is not None:
            raise EventSecurityError("payload and payload_ref are mutually exclusive")
        reference_identity = (
            None
            if payload_ref is None
            else _canonical_payload_reference_identity(payload_ref)
        )
        if payload is None and payload_ref is None:
            actual_payload: dict[str, Any] | None = {}
        elif payload is None:
            actual_payload = None
        else:
            if not isinstance(payload, Mapping):
                raise EventSecurityError("event payload must be an object")
            self._validate_reserved_payload(payload)
            payload_copy = _thaw_projector_value(payload, path="/payload")
            _collect_policy_secret_values(
                payload_copy,
                policy=policy,
                pointer="",
                collected=policy_secret_values,
            )
            actual_payload = self._project_value(payload_copy, policy=policy, pointer="")

        requires_secure_reference = actual_classification in PROTECTED_CLASSIFICATIONS
        if reference_identity is not None:
            if (
                policy.whole_document_reference
                is WholeDocumentReferenceDisposition.DENY
            ):
                raise EventSecurityError(
                    "event schema does not permit whole-document payload references"
                )
            if (
                policy.has_protected_fields
                or policy.whole_document_reference
                is WholeDocumentReferenceDisposition.SECURE_REQUIRED
            ):
                requires_secure_reference = True
            if requires_secure_reference and not policy.permits_secure_reference:
                raise EventSecurePayloadRequiredError(
                    "protected payload references require a schema-declared "
                    "secure whole-document representation"
                )

        if requires_secure_reference:
            if actual_payload not in (None, {}):
                raise EventSecurePayloadRequiredError(
                    "protected event content must not be stored inline"
                )
            self._validate_secure_reference(
                (
                    None
                    if reference_identity is None
                    else MappingProxyType(reference_identity.to_dict())
                ),
                tenant_id=actual_tenant,
                classification=actual_classification,
            )
        elif reference_identity is not None:
            if not policy.permits_ordinary_reference(
                size_bytes=reference_identity.size_bytes
            ):
                raise EventSecurityError(
                    "ordinary payload reference requires schema permission and "
                    "declared oversized non-sensitive content"
                )

        detached_reference = _detach_reference(reference_identity)

        projected_extensions = self._project_value(
            actual_extensions,
            policy=policy,
            pointer="/extensions",
        )
        redacted_fields = _redact_value(
            {
                "payload": actual_payload,
                "extensions": projected_extensions,
            },
            frozenset(),
            policy_secret_values,
        )

        return SecurityProjection(
            payload=redacted_fields["payload"],
            payload_ref=detached_reference,
            extensions=redacted_fields["extensions"],
            tenant_id=actual_tenant,
            classification=actual_classification,
        )

    def project_export(
        self,
        *,
        payload: Mapping[str, Any] | None,
        extensions: Mapping[str, Any] | None,
        policy: SensitivityPolicy,
    ) -> SecurityExportProjection:
        """Project legacy/read fields without weakening the ingress policy.

        Accepted canonical events are already projected before append. This
        method exists for bounded legacy and read projections, where exact
        secret keys must be redacted and schema-classified values must never
        be serialized merely because their key does not resemble a secret.
        """

        if payload is not None and not isinstance(payload, Mapping):
            raise EventSecurityError("export payload must be an object")
        if extensions is not None and not isinstance(extensions, Mapping):
            raise EventSecurityError("export extensions must be an object")
        actual_payload = (
            None
            if payload is None
            else _thaw_projector_value(payload, path="/payload")
        )
        actual_extensions = _thaw_projector_value(
            extensions or {},
            path="/extensions",
        )
        secret_values: set[str] = set()
        _collect_secret_values(
            {"payload": actual_payload, "extensions": actual_extensions},
            self._forbidden_secret_keys,
            secret_values,
        )
        if actual_payload is not None:
            _collect_policy_secret_values(
                actual_payload,
                policy=policy,
                pointer="",
                collected=secret_values,
            )
        _collect_policy_secret_values(
            actual_extensions,
            policy=policy,
            pointer="/extensions",
            collected=secret_values,
        )

        projected_payload = (
            None
            if actual_payload is None
            else self._project_export_value(
                actual_payload,
                policy=policy,
                pointer="",
            )
        )
        projected_extensions = self._project_export_value(
            actual_extensions,
            policy=policy,
            pointer="/extensions",
        )
        redacted = _redact_value(
            {
                "payload": projected_payload,
                "extensions": projected_extensions,
            },
            self._forbidden_secret_keys,
            secret_values,
        )
        return SecurityExportProjection(
            payload=redacted["payload"],
            extensions=redacted["extensions"],
        )

    def _project_value(
        self,
        value: Any,
        *,
        policy: SensitivityPolicy,
        pointer: str,
    ) -> Any:
        disposition = policy.disposition_for(pointer)
        if disposition is FieldDisposition.FORBIDDEN:
            raise EventSecurityError("forbidden event field", path=pointer or "/")
        if disposition is FieldDisposition.REFERENCE_ONLY:
            raise EventSecurePayloadRequiredError(
                "reference-only event field cannot be stored inline",
                path=pointer or "/",
            )
        if disposition is FieldDisposition.SENSITIVE:
            if policy.redact_sensitive:
                return "[REDACTED]"
            raise EventSecurityError(
                "sensitive event field requires an explicit protected representation",
                path=pointer or "/",
            )
        if isinstance(value, Mapping):
            projected: dict[str, Any] = {}
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    raise EventSecurityError(
                        "event object keys must be strings",
                        path=pointer or "/",
                    )
                key = str(raw_key)
                child_pointer = f"{pointer}/{_escape_pointer(key)}"
                if _normalize_key(key) in self._forbidden_secret_keys:
                    if item == "[redacted]":
                        projected[key] = item
                        continue
                    raise EventSecurityError("forbidden secret field", path=child_pointer)
                projected[key] = self._project_value(
                    item,
                    policy=policy,
                    pointer=child_pointer,
                )
            return projected
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [
                self._project_value(
                    item,
                    policy=policy,
                    pointer=f"{pointer}/{index}",
                )
                for index, item in enumerate(value)
            ]
        return copy.deepcopy(value)

    def _project_export_value(
        self,
        value: Any,
        *,
        policy: SensitivityPolicy,
        pointer: str,
    ) -> Any:
        disposition = policy.disposition_for(pointer)
        if disposition is FieldDisposition.FORBIDDEN:
            raise EventSecurityError(
                "forbidden event field cannot be exported",
                path=pointer or "/",
            )
        if disposition is FieldDisposition.REFERENCE_ONLY:
            raise EventSecurePayloadRequiredError(
                "reference-only event field cannot be exported inline",
                path=pointer or "/",
            )
        if disposition is FieldDisposition.SENSITIVE:
            if policy.redact_sensitive:
                return "[REDACTED]"
            raise EventSecurityError(
                "sensitive event field requires an explicit export representation",
                path=pointer or "/",
            )
        if isinstance(value, Mapping):
            projected: dict[str, Any] = {}
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    raise EventSecurityError(
                        "event object keys must be strings",
                        path=pointer or "/",
                    )
                key = str(raw_key)
                projected[key] = self._project_export_value(
                    item,
                    policy=policy,
                    pointer=f"{pointer}/{_escape_pointer(key)}",
                )
            return projected
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return [
                self._project_export_value(
                    item,
                    policy=policy,
                    pointer=f"{pointer}/{index}",
                )
                for index, item in enumerate(value)
            ]
        return copy.deepcopy(value)

    def _validate_reserved_extensions(self, extensions: Mapping[str, Any]) -> None:
        for raw_key, value in extensions.items():
            if not isinstance(raw_key, str):
                raise EventSecurityError(
                    "event extension keys must be strings",
                    path="/extensions",
                )
            key = str(raw_key)
            normalized_key = _normalize_key(key)
            path = f"/extensions/{_escape_pointer(key)}"
            if normalized_key in _AUTHORITY_CONTAINER_KEYS:
                self._validate_authority_container(value, path=path)
            elif normalized_key in _RESERVED_AUTHORITY_FIELDS:
                raise EventReservedFieldError(
                    "extension cannot override an infrastructure-owned event field",
                    path=path,
                )
            elif normalized_key == _LEGACY_EXTENSION_NAMESPACE:
                self._validate_legacy_authority_containers(value, path=path)

    def _validate_legacy_authority_containers(self, value: Any, *, path: str) -> None:
        if not isinstance(value, Mapping):
            raise EventSecurityError(
                "legacy extension authority namespace must be an object",
                path=path,
            )
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise EventSecurityError(
                    "legacy extension keys must be strings",
                    path=path,
                )
            key = str(raw_key)
            if _normalize_key(key) in _AUTHORITY_CONTAINER_KEYS:
                self._validate_authority_container(
                    item,
                    path=f"{path}/{_escape_pointer(key)}",
                )

    def _validate_authority_container(self, value: Any, *, path: str) -> None:
        if not isinstance(value, Mapping):
            raise EventSecurityError(
                "event authority container must be an object",
                path=path,
            )
        self._scan_authority_container(value, path=path)

    def _scan_authority_container(self, value: Any, *, path: str) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    raise EventSecurityError(
                        "event authority container keys must be strings",
                        path=path,
                    )
                key = str(raw_key)
                child_path = f"{path}/{_escape_pointer(key)}"
                if _normalize_key(key) in _RESERVED_AUTHORITY_FIELDS:
                    raise EventReservedFieldError(
                        "extension authority container cannot override an "
                        "infrastructure-owned event field",
                        path=child_path,
                    )
                self._scan_authority_container(item, path=child_path)
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, item in enumerate(value):
                self._scan_authority_container(item, path=f"{path}/{index}")

    def _validate_reserved_payload(self, payload: Mapping[str, Any]) -> None:
        for raw_key in payload:
            if not isinstance(raw_key, str):
                raise EventSecurityError(
                    "event payload keys must be strings",
                    path="/payload",
                )
            key = str(raw_key)
            if _normalize_key(key) in RESERVED_EVENT_FIELDS:
                raise EventReservedFieldError(
                    "payload cannot override an infrastructure-owned event field",
                    path=f"/payload/{_escape_pointer(key)}",
                )

    def _validate_secure_reference(
        self,
        reference: Mapping[str, Any] | None,
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> None:
        if reference is None:
            raise EventSecurePayloadRequiredError(
                "protected event content requires a secure payload reference"
            )
        if tenant_id is None:
            raise EventSecurePayloadRequiredError(
                "secure payload reference requires an authoritative tenant scope"
            )
        if self._secure_payload_store is None:
            raise EventSecurePayloadRequiredError(
                "secure payload store is not configured"
            )
        validation_failed = False
        validation: SecurePayloadValidation | None = None
        try:
            validation = self._secure_payload_store.validate_reference(
                reference,
                tenant_id=tenant_id,
                classification=classification,
            )
        except Exception:
            validation_failed = True
        if validation_failed:
            raise EventSecurePayloadRequiredError(
                "secure payload reference validation failed"
            )
        if type(validation) is not SecurePayloadValidation:
            raise EventSecurePayloadRequiredError(
                "secure payload store must return reference-bound validation"
            )
        if not validation.proves(
            reference,
            tenant_id=tenant_id,
            classification=classification,
        ):
            raise EventSecurePayloadRequiredError(
                "secure payload store validation does not match the exact "
                "reference, tenant, classification, and required capabilities"
            )


def _canonical_payload_reference_identity(
    reference: Any,
) -> _CanonicalPayloadReferenceIdentity:
    if isinstance(reference, Mapping):
        if any(not isinstance(key, str) for key in reference):
            raise EventSecurityError(
                "payload reference fields must be canonical strings",
                path="/payload_ref",
            )
        source = dict(reference)
    elif is_dataclass(reference) and not isinstance(reference, type):
        source = {
            field.name: getattr(reference, field.name)
            for field in fields(reference)
        }
    else:
        raise EventSecurityError(
            "payload reference must use the canonical reference contract",
            path="/payload_ref",
        )

    actual_fields = frozenset(source)
    unknown = actual_fields - _CANONICAL_PAYLOAD_REFERENCE_FIELDS
    if unknown:
        field_name = sorted(unknown)[0]
        raise EventSecurityError(
            "payload reference contains an unknown authority field",
            path=f"/payload_ref/{_escape_pointer(field_name)}",
        )
    missing = _CANONICAL_PAYLOAD_REFERENCE_FIELDS - actual_fields
    if missing:
        field_name = sorted(missing)[0]
        raise EventSecurityError(
            "payload reference is missing a canonical field",
            path=f"/payload_ref/{_escape_pointer(field_name)}",
        )

    uri = source["uri"]
    checksum = source["expected_checksum"]
    content_type = source["content_type"]
    size_bytes = source["size_bytes"]
    if not isinstance(uri, str) or not uri.strip():
        raise EventSecurityError(
            "payload reference uri is required",
            path="/payload_ref/uri",
        )
    if (
        not isinstance(checksum, str)
        or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", checksum.strip()) is None
    ):
        raise EventSecurityError(
            "payload reference sha256 checksum is required",
            path="/payload_ref/expected_checksum",
        )
    if not isinstance(content_type, str) or not content_type.strip():
        raise EventSecurityError(
            "payload reference content_type is required",
            path="/payload_ref/content_type",
        )
    if size_bytes is not None:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise EventSecurityError(
                "payload reference size_bytes must be an integer",
                path="/payload_ref/size_bytes",
            )
        if size_bytes < 0:
            raise EventSecurityError(
                "payload reference size_bytes must be non-negative",
                path="/payload_ref/size_bytes",
            )
    return _CanonicalPayloadReferenceIdentity(
        uri=uri.strip(),
        expected_checksum=checksum.strip().lower(),
        content_type=content_type.strip(),
        size_bytes=size_bytes,
    )


def _detach_reference(
    reference: _CanonicalPayloadReferenceIdentity | None,
) -> dict[str, Any] | None:
    if reference is None:
        return None
    return reference.to_dict()


def _normalize_key(key: str) -> str:
    return str(key).strip().casefold().replace("-", "_")


def _escape_pointer(value: str) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EventSecurityError("tenant_id must be a string")
    text = value.strip()
    return text or None


def _thaw_projector_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventSecurityError("event object keys must be strings", path=path)
            result[key] = _thaw_projector_value(item, path=f"{path}/{_escape_pointer(key)}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _thaw_projector_value(item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventSecurityError("event number must be finite", path=path)
        return 0 if value == 0 else value
    raise EventSecurityError(
        f"unsupported event value type: {type(value).__name__}",
        path=path,
    )


def _freeze_projected_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            key: _freeze_projected_value(item)
            for key, item in value.items()
        }
    )


def _freeze_projected_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_projected_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_projected_value(item) for item in value)
    return value


def _collect_secret_values(
    value: Any,
    forbidden_keys: frozenset[str],
    collected: set[str],
) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if _normalize_key(str(raw_key)) in forbidden_keys:
                _collect_string_leaves(item, collected)
                continue
            _collect_secret_values(item, forbidden_keys, collected)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_secret_values(item, forbidden_keys, collected)


def _collect_policy_secret_values(
    value: Any,
    *,
    policy: SensitivityPolicy,
    pointer: str,
    collected: set[str],
) -> None:
    disposition = policy.disposition_for(pointer)
    if disposition is FieldDisposition.SENSITIVE:
        _collect_string_leaves(value, collected)
        return
    if disposition in {
        FieldDisposition.REFERENCE_ONLY,
        FieldDisposition.FORBIDDEN,
    }:
        return
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            _collect_policy_secret_values(
                item,
                policy=policy,
                pointer=f"{pointer}/{_escape_pointer(str(raw_key))}",
                collected=collected,
            )
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for index, item in enumerate(value):
            _collect_policy_secret_values(
                item,
                policy=policy,
                pointer=f"{pointer}/{index}",
                collected=collected,
            )


def _collect_string_leaves(value: Any, collected: set[str]) -> None:
    if isinstance(value, str):
        if value:
            collected.add(value)
            if value.casefold().startswith("bearer "):
                bearer_value = value[7:].strip()
                if bearer_value:
                    collected.add(bearer_value)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _collect_string_leaves(item, collected)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_string_leaves(item, collected)


def _redact_value(
    value: Any,
    forbidden_keys: frozenset[str],
    secret_values: set[str],
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(raw_key): (
                "[REDACTED]"
                if _normalize_key(str(raw_key)) in forbidden_keys
                else _redact_value(item, forbidden_keys, secret_values)
            )
            for raw_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_value(item, forbidden_keys, secret_values) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in sorted(secret_values, key=len, reverse=True):
            redacted = re.sub(re.escape(secret), "[REDACTED]", redacted, flags=re.IGNORECASE)
        return redacted
    return copy.deepcopy(value)
