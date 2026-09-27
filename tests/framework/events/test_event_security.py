from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from framework.events.errors import (
    EventReservedFieldError,
    EventSecurePayloadRequiredError,
    EventSecurityError,
)
from framework.events.schema import (
    EventSecurityProjector,
    REQUIRED_SECURE_PAYLOAD_CAPABILITIES,
    SecurePayloadCapability,
    SecurePayloadValidation,
    SecurityClassification,
    SensitivityPolicy,
    WholeDocumentReferenceDisposition,
)


@dataclass(frozen=True)
class _Reference:
    uri: str = "secure://tenant/event-payload"
    expected_checksum: str = "sha256:" + "a" * 64
    content_type: str = "application/json"
    size_bytes: int | None = 128 * 1024


class _SecureStore:
    def __init__(
        self,
        *,
        validation_changes: Mapping[str, object] | None = None,
        capabilities: frozenset[SecurePayloadCapability | str] = (
            REQUIRED_SECURE_PAYLOAD_CAPABILITIES
        ),
        return_unbound: bool = False,
    ) -> None:
        self.validation_changes = dict(validation_changes or {})
        self.capabilities = capabilities
        self.return_unbound = return_unbound
        self.calls: list[
            tuple[dict[str, object], str | None, SecurityClassification]
        ] = []

    def validate_reference(
        self,
        reference: Mapping[str, object],
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> SecurePayloadValidation | object:
        canonical_reference = dict(reference)
        self.calls.append((canonical_reference, tenant_id, classification))
        if self.return_unbound:
            return object()
        assert tenant_id is not None
        values: dict[str, object] = {
            **canonical_reference,
            "tenant_id": tenant_id,
            "classification": classification,
            "capabilities": self.capabilities,
        }
        values.update(self.validation_changes)
        return SecurePayloadValidation(**values)  # type: ignore[arg-type]


class _LeakySecureStore:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def validate_reference(
        self,
        reference: Mapping[str, object],
        *,
        tenant_id: str,
        classification: SecurityClassification,
    ) -> SecurePayloadValidation:
        del reference, tenant_id, classification
        raise EventSecurityError(f"secure store failed with {self.secret}")


def _ordinary_reference_policy() -> SensitivityPolicy:
    return SensitivityPolicy(
        whole_document_reference=(
            WholeDocumentReferenceDisposition.NON_SENSITIVE
        )
    )


def _secure_reference_policy(
    *,
    field_rules: Mapping[str, str] | None = None,
) -> SensitivityPolicy:
    return SensitivityPolicy(
        field_rules=field_rules or {},
        whole_document_reference=(
            WholeDocumentReferenceDisposition.SECURE_REQUIRED
        ),
    )


def test_whole_document_reference_policy_rejects_boolean_guessing() -> None:
    with pytest.raises(TypeError, match="WholeDocumentReferenceDisposition"):
        SensitivityPolicy(whole_document_reference=True)  # type: ignore[arg-type]


def test_projector_rejects_reserved_extension_override() -> None:
    with pytest.raises(EventReservedFieldError):
        EventSecurityProjector().project(
            payload={"safe": True},
            payload_ref=None,
            extensions={"tenant_id": "forged"},
            policy=SensitivityPolicy(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant_id": "forged"},
        {"security-classification": "public"},
        {"event_type": "forged"},
    ],
)
def test_projector_rejects_reserved_payload_scope(payload: dict[str, str]) -> None:
    with pytest.raises(EventReservedFieldError):
        EventSecurityProjector().project(
            payload=payload,
            payload_ref=None,
            extensions={},
            policy=SensitivityPolicy(),
        )


@pytest.mark.parametrize("container", [{1: "a", "1": "b"}, {1: "a"}])
def test_projector_rejects_non_string_object_keys(container: dict[object, str]) -> None:
    with pytest.raises(EventSecurityError):
        EventSecurityProjector().project(
            payload=container,
            payload_ref=None,
            extensions={},
            policy=SensitivityPolicy(),
        )


def test_projector_rejects_exact_secret_keys_without_substring_false_positive() -> None:
    secret = "must-not-appear-in-diagnostic"
    projector = EventSecurityProjector()

    safe = projector.project(
        payload={"token_count": 5, "secretary": "Ada"},
        payload_ref=None,
        extensions={},
        policy=SensitivityPolicy(),
    )
    assert safe.payload == {"token_count": 5, "secretary": "Ada"}

    with pytest.raises(EventSecurityError) as caught:
        projector.project(
            payload={"nested": {"api-key": secret}},
            payload_ref=None,
            extensions={},
            policy=SensitivityPolicy(),
        )
    assert caught.value.path == "/nested/api-key"
    assert secret not in str(caught.value)


def test_projector_accepts_only_canonical_redaction_for_secret_keys() -> None:
    projector = EventSecurityProjector()

    projected = projector.project(
        payload={"nested": {"api-key": "[redacted]", "safe": "ok"}},
        payload_ref=None,
        extensions={},
        policy=SensitivityPolicy(),
    )
    assert projected.payload == {
        "nested": {"api-key": "[redacted]", "safe": "ok"}
    }

    for forged_redaction in ("[REDACTED]", " [redacted]", "[redacted]\n"):
        with pytest.raises(EventSecurityError, match="forbidden secret field"):
            projector.project(
                payload={"nested": {"api-key": forged_redaction}},
                payload_ref=None,
                extensions={},
                policy=SensitivityPolicy(),
            )


def test_sensitive_field_is_rejected_or_explicitly_redacted_by_schema_policy() -> None:
    projector = EventSecurityProjector()
    with pytest.raises(EventSecurityError):
        projector.project(
            payload={"answer": "private"},
            payload_ref=None,
            extensions={},
            policy=SensitivityPolicy(field_rules={"/answer": "sensitive"}),
        )

    projected = projector.project(
        payload={"answer": "private", "safe": True},
        payload_ref=None,
        extensions={},
        policy=SensitivityPolicy(
            field_rules={"/answer": "sensitive"},
            redact_sensitive=True,
        ),
    )
    assert projected.payload == {"answer": "[REDACTED]", "safe": True}


@pytest.mark.parametrize(
    "classification",
    [SecurityClassification.CONFIDENTIAL, SecurityClassification.RESTRICTED],
)
def test_protected_content_fails_without_secure_payload_store(
    classification: SecurityClassification,
) -> None:
    with pytest.raises(EventSecurePayloadRequiredError):
        EventSecurityProjector().project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            tenant_id="tenant-1",
            classification=classification,
            policy=_secure_reference_policy(),
        )


def test_secure_reference_still_requires_schema_reference_permission() -> None:
    with pytest.raises(EventSecurityError, match="does not permit"):
        EventSecurityProjector().project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            tenant_id="tenant-1",
            classification=SecurityClassification.CONFIDENTIAL,
            policy=SensitivityPolicy(),
        )


def test_ordinary_reference_is_allowed_only_for_schema_permitted_non_sensitive_data() -> None:
    reference = _Reference(uri="artifact://run-1/large.json")

    with pytest.raises(EventSecurityError):
        EventSecurityProjector().project(
            payload=None,
            payload_ref=reference,
            extensions={},
            policy=SensitivityPolicy(),
        )

    projected = EventSecurityProjector().project(
        payload=None,
        payload_ref=reference,
        extensions={},
        policy=_ordinary_reference_policy(),
    )
    assert projected.payload_ref == {
        "uri": reference.uri,
        "expected_checksum": reference.expected_checksum,
        "content_type": "application/json",
        "size_bytes": reference.size_bytes,
    }

    for unproven_size in (None, 64 * 1024):
        with pytest.raises(EventSecurityError):
            EventSecurityProjector().project(
                payload=None,
                payload_ref=_Reference(size_bytes=unproven_size),
                extensions={},
                policy=_ordinary_reference_policy(),
            )


@pytest.mark.parametrize(
    "protected_disposition",
    ["sensitive", "reference_only", "forbidden"],
)
def test_ordinary_reference_policy_rejects_any_protected_path(
    protected_disposition: str,
) -> None:
    with pytest.raises(ValueError, match="non-sensitive"):
        SensitivityPolicy(
            field_rules={"/protected": protected_disposition},
            whole_document_reference=(
                WholeDocumentReferenceDisposition.NON_SENSITIVE
            ),
        )


@pytest.mark.parametrize(
    ("classification", "disposition", "accepted"),
    [
        (SecurityClassification.PUBLIC, "non_sensitive", True),
        (SecurityClassification.INTERNAL, "non_sensitive", True),
        (SecurityClassification.CONFIDENTIAL, "non_sensitive", False),
        (SecurityClassification.RESTRICTED, "non_sensitive", False),
        (SecurityClassification.PUBLIC, "secure_required", True),
        (SecurityClassification.INTERNAL, "secure_required", True),
        (SecurityClassification.CONFIDENTIAL, "secure_required", True),
        (SecurityClassification.RESTRICTED, "secure_required", True),
    ],
)
def test_whole_document_reference_classification_matrix(
    classification: SecurityClassification,
    disposition: str,
    accepted: bool,
) -> None:
    policy = SensitivityPolicy(whole_document_reference=disposition)
    store = _SecureStore()
    projector = EventSecurityProjector(secure_payload_store=store)
    operation = lambda: projector.project(
        payload=None,
        payload_ref=_Reference(),
        extensions={},
        tenant_id="tenant-1",
        classification=classification,
        policy=policy,
    )
    if accepted:
        assert operation().payload_ref is not None
    elif classification in {
        SecurityClassification.CONFIDENTIAL,
        SecurityClassification.RESTRICTED,
    }:
        with pytest.raises(EventSecurePayloadRequiredError, match="schema-declared"):
            operation()
    else:  # pragma: no cover - table documents the complete product
        raise AssertionError("invalid matrix case")


def test_secure_store_validation_is_bound_to_exact_reference_and_scope() -> None:
    store = _SecureStore()
    reference = _Reference()
    projected = EventSecurityProjector(secure_payload_store=store).project(
        payload=None,
        payload_ref=reference,
        extensions={},
        tenant_id="tenant-1",
        classification=SecurityClassification.CONFIDENTIAL,
        policy=_secure_reference_policy(),
    )
    assert projected.payload_ref == {
        "uri": reference.uri,
        "expected_checksum": reference.expected_checksum,
        "content_type": "application/json",
        "size_bytes": reference.size_bytes,
    }
    assert store.calls == [
        (
            {
                "uri": reference.uri,
                "expected_checksum": reference.expected_checksum,
                "content_type": reference.content_type,
                "size_bytes": reference.size_bytes,
            },
            "tenant-1",
            SecurityClassification.CONFIDENTIAL,
        )
    ]


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("uri", "secure://tenant/other"),
        ("expected_checksum", "sha256:" + "b" * 64),
        ("content_type", "application/octet-stream"),
        ("size_bytes", 256 * 1024),
        ("tenant_id", "tenant-other"),
        ("classification", SecurityClassification.RESTRICTED),
    ],
)
def test_secure_store_validation_rejects_any_exact_binding_mismatch(
    changed_field: str,
    changed_value: object,
) -> None:
    projector = EventSecurityProjector(
        secure_payload_store=_SecureStore(
            validation_changes={changed_field: changed_value}
        )
    )
    with pytest.raises(EventSecurePayloadRequiredError, match="exact reference"):
        projector.project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            tenant_id="tenant-1",
            classification=SecurityClassification.CONFIDENTIAL,
            policy=_secure_reference_policy(),
        )


def test_secure_store_cannot_return_an_unbound_capability_claim() -> None:
    projector = EventSecurityProjector(
        secure_payload_store=_SecureStore(return_unbound=True)
    )
    with pytest.raises(EventSecurePayloadRequiredError, match="reference-bound"):
        projector.project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            tenant_id="tenant-1",
            classification=SecurityClassification.CONFIDENTIAL,
            policy=_secure_reference_policy(),
        )


def test_secure_store_failure_is_not_retained_in_the_exception_chain() -> None:
    secret = "secure-store-password-must-not-leak"
    projector = EventSecurityProjector(
        secure_payload_store=_LeakySecureStore(secret)
    )

    with pytest.raises(EventSecurePayloadRequiredError) as caught:
        projector.project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            tenant_id="tenant-1",
            classification=SecurityClassification.CONFIDENTIAL,
            policy=_secure_reference_policy(),
        )

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "reference",
    [
        {
            "reference": "secure://tenant/event-payload",
            "checksum": "sha256:" + "a" * 64,
            "content_type": "application/json",
            "size_bytes": 128 * 1024,
        },
        {
            "uri": "secure://tenant/event-payload",
            "expected_checksum": "sha256:" + "a" * 64,
            "content_type": "application/json",
            "size_bytes": 128 * 1024,
            "tenant_id": "tenant-1",
        },
    ],
)
def test_payload_reference_rejects_alias_and_unknown_authority_fields(
    reference: dict[str, object],
) -> None:
    with pytest.raises(EventSecurityError, match="unknown authority field"):
        EventSecurityProjector().project(
            payload=None,
            payload_ref=reference,
            extensions={},
            policy=_ordinary_reference_policy(),
        )


def test_reference_only_policy_fails_closed_without_secure_store() -> None:
    with pytest.raises(EventSecurePayloadRequiredError):
        EventSecurityProjector().project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            policy=SensitivityPolicy(
                field_rules={"/body": "reference_only"},
                whole_document_reference=(
                    WholeDocumentReferenceDisposition.SECURE_REQUIRED
                ),
            ),
        )


def test_reference_only_field_is_optional_but_secure_when_present() -> None:
    policy = SensitivityPolicy(
        field_rules={"/items/*/body": "reference_only"},
        whole_document_reference=(
            WholeDocumentReferenceDisposition.SECURE_REQUIRED
        ),
    )
    projected = EventSecurityProjector().project(
        payload={"items": [{"title": "safe"}]},
        payload_ref=None,
        extensions={},
        policy=policy,
    )
    assert projected.payload == {"items": ({"title": "safe"},)}

    with pytest.raises(EventSecurePayloadRequiredError) as caught:
        EventSecurityProjector().project(
            payload={"items": [{"body": "protected"}]},
            payload_ref=None,
            extensions={},
            policy=policy,
        )
    assert caught.value.path == "/items/0/body"
    assert "protected" not in str(caught.value)


def test_secure_reference_requires_authoritative_tenant_scope() -> None:
    store = _SecureStore()
    with pytest.raises(EventSecurePayloadRequiredError, match="tenant scope"):
        EventSecurityProjector(secure_payload_store=store).project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            classification=SecurityClassification.CONFIDENTIAL,
            policy=_secure_reference_policy(),
        )
    assert store.calls == []


@pytest.mark.parametrize(
    "missing_capability",
    [
        "tenant_authorization",
        "encryption_in_transit",
        "encryption_at_rest",
        "integrity_verification",
        "audited_access",
    ],
)
def test_secure_reference_requires_each_capability(missing_capability: str) -> None:
    capabilities = set(REQUIRED_SECURE_PAYLOAD_CAPABILITIES)
    capabilities.remove(SecurePayloadCapability(missing_capability))
    store = _SecureStore(capabilities=frozenset(capabilities))

    with pytest.raises(EventSecurePayloadRequiredError, match="capabilities"):
        EventSecurityProjector(secure_payload_store=store).project(
            payload=None,
            payload_ref=_Reference(),
            extensions={},
            tenant_id="tenant-1",
            classification=SecurityClassification.RESTRICTED,
            policy=_secure_reference_policy(),
        )
    assert len(store.calls) == 1


def test_tenant_and_classification_are_authoritative_and_propagated() -> None:
    projected = EventSecurityProjector().project(
        payload={"safe": True},
        payload_ref=None,
        extensions={"io.newsroom.feature": "enabled"},
        tenant_id=" tenant-1 ",
        classification=SecurityClassification.INTERNAL,
        policy=SensitivityPolicy(),
    )
    assert projected.tenant_id == "tenant-1"
    assert projected.classification is SecurityClassification.INTERNAL

    for container in (
        {"Tenant-ID": "forged"},
        {"SECURITY_CLASSIFICATION": "public"},
    ):
        with pytest.raises(EventReservedFieldError):
            EventSecurityProjector().project(
                payload={"safe": True},
                payload_ref=None,
                extensions=container,
                tenant_id="tenant-1",
                policy=SensitivityPolicy(),
            )


def test_extension_policy_redacts_schema_sensitive_values_and_duplicates() -> None:
    secret = "extension-sensitive-value"
    projected = EventSecurityProjector().project(
        payload={"diagnostic": f"operation failed with {secret}"},
        payload_ref=None,
        extensions={"reason": secret},
        policy=SensitivityPolicy(
            field_rules={"/extensions/reason": "sensitive"},
            redact_sensitive=True,
        ),
    )

    assert projected.extensions["reason"] == "[REDACTED]"
    assert secret not in projected.payload["diagnostic"]


def test_export_projection_is_schema_aware_and_redacts_duplicate_values() -> None:
    secret = "schema-sensitive-export-value"
    projected = EventSecurityProjector().project_export(
        payload={"resume_metadata": {"credential": secret}},
        extensions={
            "diagnostic": f"resume failed for {secret}",
            "authorization": f"Bearer {secret}",
        },
        policy=SensitivityPolicy(
            field_rules={"/resume_metadata": "sensitive"},
            redact_sensitive=True,
        ),
    )

    assert projected.payload["resume_metadata"] == "[REDACTED]"
    assert secret not in projected.extensions["diagnostic"]
    assert projected.extensions["authorization"] == "[REDACTED]"


def test_export_projection_rejects_inline_reference_only_content() -> None:
    with pytest.raises(EventSecurePayloadRequiredError, match="cannot be exported"):
        EventSecurityProjector().project_export(
            payload={"stream_event": {"chunk": "raw"}},
            extensions={},
            policy=SensitivityPolicy(
                field_rules={"/stream_event": "reference_only"},
                whole_document_reference=(
                    WholeDocumentReferenceDisposition.SECURE_REQUIRED
                ),
            ),
        )


@pytest.mark.parametrize(
    ("extensions", "expected_path"),
    [
        (
            {"metadata": {"nested": {"tenant_id": "forged"}}},
            "/extensions/metadata/nested/tenant_id",
        ),
        (
            {"envelope": {"items": [{"security-classification": "public"}]}},
            "/extensions/envelope/items/0/security-classification",
        ),
        (
            {
                "io.newsroom.legacy": {
                    "metadata": {"nested": {"event_id": "forged"}}
                }
            },
            "/extensions/io.newsroom.legacy/metadata/nested/event_id",
        ),
        (
            {
                "io.newsroom.legacy": {
                    "envelope": {"nested": {"trace_id": "forged"}}
                }
            },
            "/extensions/io.newsroom.legacy/envelope/nested/trace_id",
        ),
    ],
)
def test_authority_containers_recursively_reject_reserved_overrides(
    extensions: dict[str, object],
    expected_path: str,
) -> None:
    with pytest.raises(EventReservedFieldError) as caught:
        EventSecurityProjector().project(
            payload={"safe": True},
            payload_ref=None,
            extensions=extensions,
            tenant_id="tenant-1",
            policy=SensitivityPolicy(),
        )
    assert caught.value.path == expected_path


@pytest.mark.parametrize(
    "extensions",
    [
        {"metadata": "tenant_id=forged"},
        {"envelope": ["security_classification=public"]},
        {"io.newsroom.legacy": "event_id=forged"},
    ],
)
def test_authority_containers_fail_closed_when_not_objects(
    extensions: dict[str, object],
) -> None:
    with pytest.raises(EventSecurityError, match="must be an object"):
        EventSecurityProjector().project(
            payload={"safe": True},
            payload_ref=None,
            extensions=extensions,
            policy=SensitivityPolicy(),
        )


def test_reserved_guard_does_not_scan_arbitrary_business_nesting() -> None:
    projected = EventSecurityProjector().project(
        payload={
            "document": {
                "tenant_id": "business-value",
                "metadata": {"event_type": "business-event"},
            }
        },
        payload_ref=None,
        extensions={
            "io.newsroom.feature": {
                "metadata": {"event_id": "business-value"},
                "envelope": {"security_classification": "business-label"},
            }
        },
        policy=SensitivityPolicy(),
    )

    assert projected.payload["document"]["tenant_id"] == "business-value"
    assert (
        projected.extensions["io.newsroom.feature"]["metadata"]["event_id"]
        == "business-value"
    )


@pytest.mark.parametrize("reserved_alias", ["event", "event_envelope"])
def test_legacy_envelope_aliases_are_reserved_not_extension_containers(
    reserved_alias: str,
) -> None:
    with pytest.raises(EventReservedFieldError):
        EventSecurityProjector().project(
            payload={"safe": True},
            payload_ref=None,
            extensions={reserved_alias: {"safe": True}},
            policy=SensitivityPolicy(),
        )


def test_redacted_diagnostic_removes_secret_values_without_key_substring_false_positive() -> None:
    from framework.events.schema import redact_event_value

    secret = "CaseSensitiveCredential-42"
    projected = redact_event_value(
        {
            "authorization": f"Bearer {secret}",
            "message": f"request failed for casesensitivecredential-42",
            "token_count": 9,
            "secretary": "Ada",
        }
    )

    assert projected["authorization"] == "[REDACTED]"
    assert secret.casefold() not in projected["message"].casefold()
    assert projected["token_count"] == 9
    assert projected["secretary"] == "Ada"


def test_projected_content_is_detached_from_caller_mutation() -> None:
    source = {"nested": {"items": [1, 2]}}
    extensions = {"io.newsroom.safe": {"enabled": True}}
    projected = EventSecurityProjector().project(
        payload=source,
        payload_ref=None,
        extensions=extensions,
        policy=SensitivityPolicy(),
    )
    source["nested"]["items"].append(3)
    extensions["io.newsroom.safe"]["enabled"] = False

    assert projected.payload == {"nested": {"items": (1, 2)}}
    assert projected.extensions == {"io.newsroom.safe": {"enabled": True}}
    with pytest.raises(TypeError):
        projected.payload["nested"]["items"] += (3,)  # type: ignore[index,operator]
    with pytest.raises(TypeError):
        projected.extensions["io.newsroom.safe"]["enabled"] = False  # type: ignore[index]


def test_projector_accepts_existing_canonical_immutable_views() -> None:
    from framework.events.canonical import normalize_canonical_json

    payload = normalize_canonical_json({"nested": {"items": [1, 2]}})
    extensions = normalize_canonical_json({"io.newsroom.safe": {"enabled": True}})
    projected = EventSecurityProjector().project(
        payload=payload,
        payload_ref=None,
        extensions=extensions,
        policy=SensitivityPolicy(),
    )

    assert projected.payload == {"nested": {"items": (1, 2)}}
