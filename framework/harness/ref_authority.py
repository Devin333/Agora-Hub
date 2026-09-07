"""Harness-owned reference identity and authorization boundary.

References are deliberately modeled separately from artifact and memory stores.
Stores resolve bytes or records; this module decides whether a caller may use a
particular immutable reference in the current run and stage.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import identifier, reference, required_text


REF_DESCRIPTOR_SCHEMA = "newsroom.harness-ref-descriptor/v1"
REF_POLICY_SCHEMA = "newsroom.harness-ref-policy/v1"
REF_ACCESS_READ_ONLY = "READ_ONLY"
REF_ACCESS_READ_WRITE = "READ_WRITE"
REF_SCOPE_PRIVATE = "PRIVATE"
REF_SCOPE_SHARED_READ_ONLY = "SHARED_READ_ONLY"
REF_KIND_INPUT = "input"
REF_KIND_RESULT = "result"
REF_KIND_PLANNING = "planning"
REF_KIND_MEMORY = "memory"
REF_KINDS = frozenset({REF_KIND_INPUT, REF_KIND_RESULT, REF_KIND_PLANNING, REF_KIND_MEMORY})
_CHECKSUM_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RefAccessMode(StrEnum):
    READ_ONLY = REF_ACCESS_READ_ONLY
    READ_WRITE = REF_ACCESS_READ_WRITE


class RefScope(StrEnum):
    PRIVATE = REF_SCOPE_PRIVATE
    SHARED_READ_ONLY = REF_SCOPE_SHARED_READ_ONLY


def _ref_error(message: str, *, code: str = "REF_UNAUTHORIZED", **details: Any) -> HarnessValidationError:
    return HarnessValidationError(message, code=code, details=details or None)


def _checksum(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _CHECKSUM_PATTERN.fullmatch(value) is None:
        raise HarnessValidationError(
            f"{field_name} must be a lowercase sha256 checksum",
            code="REF_INVALID_CHECKSUM",
            details={"field": field_name},
        )
    return value


def _text(value: Any, field_name: str) -> str:
    try:
        return required_text(value, field_name)
    except Exception as exc:
        raise HarnessValidationError(
            f"{field_name} is required",
            code="REF_INVALID_DESCRIPTOR",
            details={"field": field_name},
        ) from exc


def _stable_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HarnessValidationError(
            f"{field_name} must be an array",
            code="REF_INVALID_POLICY",
            details={"field": field_name},
        )
    normalized = tuple(_text(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise HarnessValidationError(
            f"{field_name} must not contain duplicates",
            code="REF_INVALID_POLICY",
            details={"field": field_name},
        )
    return tuple(sorted(normalized))


def _reference_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HarnessValidationError(
            f"{field_name} must be an array",
            code="REF_INVALID_POLICY",
            details={"field": field_name},
        )
    normalized = tuple(reference(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise HarnessValidationError(
            f"{field_name} must not contain duplicates",
            code="REF_INVALID_POLICY",
            details={"field": field_name},
        )
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class RefDescriptor:
    """Immutable logical reference identity supplied by a trusted resolver."""

    ref: str
    run_id: str
    stage_id: str
    tenant_id: str
    owner_id: str
    access_mode: RefAccessMode | str
    artifact_type: str
    source_checksum: str
    ref_kind: str
    namespace: str | None = None
    scope: RefScope | str = RefScope.PRIVATE
    schema_version: str = REF_DESCRIPTOR_SCHEMA
    descriptor_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != REF_DESCRIPTOR_SCHEMA:
            raise _ref_error(
                "unsupported reference descriptor schema",
                code="REF_SCHEMA_UNSUPPORTED",
            )
        object.__setattr__(self, "ref", reference(self.ref, "ref"))
        for field_name in ("run_id", "stage_id", "tenant_id", "owner_id"):
            object.__setattr__(self, field_name, identifier(getattr(self, field_name), field_name))
        try:
            access_mode = RefAccessMode(self.access_mode)
            scope = RefScope(self.scope)
        except (TypeError, ValueError) as exc:
            raise _ref_error("reference access or scope is invalid", code="REF_INVALID_DESCRIPTOR") from exc
        kind = _text(self.ref_kind, "ref_kind").casefold()
        if kind not in REF_KINDS:
            raise _ref_error("reference kind is unsupported", code="REF_INVALID_DESCRIPTOR", ref_kind=kind)
        artifact_type = _text(self.artifact_type, "artifact_type")
        namespace = None if self.namespace is None else _text(self.namespace, "namespace")
        if kind == REF_KIND_MEMORY and namespace is None:
            raise _ref_error("memory references require a namespace", code="REF_INVALID_DESCRIPTOR")
        if kind != REF_KIND_MEMORY and namespace is not None:
            raise _ref_error("only memory references may carry a namespace", code="REF_INVALID_DESCRIPTOR")
        source_checksum = _checksum(self.source_checksum, "source_checksum")
        if scope is RefScope.SHARED_READ_ONLY and access_mode is not RefAccessMode.READ_ONLY:
            raise _ref_error(
                "shared references must be read-only",
                code="REF_INVALID_DESCRIPTOR",
            )
        object.__setattr__(self, "access_mode", access_mode)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "ref_kind", kind)
        object.__setattr__(self, "artifact_type", artifact_type)
        object.__setattr__(self, "source_checksum", source_checksum)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "descriptor_checksum", checksum_for(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ref": self.ref,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "access_mode": self.access_mode.value,
            "artifact_type": self.artifact_type,
            "source_checksum": self.source_checksum,
            "ref_kind": self.ref_kind,
            "namespace": self.namespace,
            "scope": self.scope.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "descriptor_checksum": self.descriptor_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RefDescriptor":
        if not isinstance(value, Mapping):
            raise _ref_error("reference descriptor must be an object", code="REF_INVALID_DESCRIPTOR")
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = sorted(set(value) - expected)
        missing = sorted(expected - set(value) - {"descriptor_checksum"})
        if unknown or missing:
            raise _ref_error(
                "reference descriptor fields are invalid",
                code="REF_INVALID_DESCRIPTOR",
                unknown=unknown,
                missing=missing,
            )
        supplied = _checksum(value.get("descriptor_checksum"), "descriptor_checksum")
        payload = dict(value)
        payload.pop("descriptor_checksum", None)
        descriptor = cls(**payload)
        if supplied != descriptor.descriptor_checksum:
            raise _ref_error(
                "reference descriptor checksum does not match its identity",
                code="REF_CHECKSUM_MISMATCH",
                ref=descriptor.ref,
            )
        return descriptor

    @classmethod
    def memory(
        cls,
        *,
        namespace: str,
        ref: str,
        run_id: str,
        stage_id: str,
        tenant_id: str,
        owner_id: str,
        source_checksum: str,
        access_mode: RefAccessMode | str = RefAccessMode.READ_ONLY,
        scope: RefScope | str = RefScope.PRIVATE,
    ) -> "RefDescriptor":
        return cls(
            ref=ref,
            run_id=run_id,
            stage_id=stage_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            access_mode=access_mode,
            artifact_type="memory_namespace",
            source_checksum=source_checksum,
            ref_kind=REF_KIND_MEMORY,
            namespace=namespace,
            scope=scope,
        )


@dataclass(frozen=True, slots=True)
class RefAccessPolicy:
    """Pinned authority inputs; candidates cannot mutate or extend this object."""

    policy_id: str
    version: str
    run_id: str
    stage_id: str
    tenant_id: str
    owner_id: str
    allowed_refs: tuple[str, ...] = ()
    allowed_artifact_types: tuple[str, ...] = ()
    allowed_ref_kinds: tuple[str, ...] = tuple(sorted(REF_KINDS))
    allowed_memory_namespaces: tuple[str, ...] = ()
    shared_read_only_refs: tuple[str, ...] = ()
    writable_refs: tuple[str, ...] = ()
    pinned_checksums: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = REF_POLICY_SCHEMA
    policy_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != REF_POLICY_SCHEMA:
            raise _ref_error("unsupported reference policy schema", code="REF_SCHEMA_UNSUPPORTED")
        object.__setattr__(self, "policy_id", identifier(self.policy_id, "policy_id"))
        object.__setattr__(self, "version", identifier(self.version, "version"))
        for field_name in ("run_id", "stage_id", "tenant_id", "owner_id"):
            object.__setattr__(self, field_name, identifier(getattr(self, field_name), field_name))
        allowed_refs = _reference_tuple(self.allowed_refs, "allowed_refs")
        allowed_types = _stable_tuple(self.allowed_artifact_types, "allowed_artifact_types")
        allowed_kinds = _stable_tuple(self.allowed_ref_kinds, "allowed_ref_kinds")
        if not set(allowed_kinds).issubset(REF_KINDS):
            raise _ref_error("allowed_ref_kinds contains an unsupported kind", code="REF_INVALID_POLICY")
        namespaces = _stable_tuple(self.allowed_memory_namespaces, "allowed_memory_namespaces")
        shared = _reference_tuple(self.shared_read_only_refs, "shared_read_only_refs")
        writable = _reference_tuple(self.writable_refs, "writable_refs")
        if not set(shared).issubset(allowed_refs):
            raise _ref_error("shared references must be in the pinned allowlist", code="REF_INVALID_POLICY")
        if not set(writable).issubset(allowed_refs):
            raise _ref_error("writable references must be in the pinned allowlist", code="REF_INVALID_POLICY")
        if not isinstance(self.pinned_checksums, Mapping):
            raise _ref_error("pinned_checksums must be an object", code="REF_INVALID_POLICY")
        checksums: dict[str, str] = {}
        for ref, checksum in self.pinned_checksums.items():
            normalized_ref = reference(ref, "pinned_checksums.ref")
            if normalized_ref not in allowed_refs:
                raise _ref_error("pinned checksum reference is not allowlisted", code="REF_INVALID_POLICY", ref=normalized_ref)
            checksums[normalized_ref] = _checksum(checksum, "pinned_checksums.checksum")
        object.__setattr__(self, "allowed_refs", allowed_refs)
        object.__setattr__(self, "allowed_artifact_types", allowed_types)
        object.__setattr__(self, "allowed_ref_kinds", allowed_kinds)
        object.__setattr__(self, "allowed_memory_namespaces", namespaces)
        object.__setattr__(self, "shared_read_only_refs", shared)
        object.__setattr__(self, "writable_refs", writable)
        object.__setattr__(self, "pinned_checksums", MappingProxyType(dict(sorted(checksums.items()))))
        object.__setattr__(self, "policy_checksum", checksum_for(self.checksum_projection()))

    @property
    def exact_ref(self) -> str:
        return f"{self.policy_id}@{self.version}"

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "allowed_refs": list(self.allowed_refs),
            "allowed_artifact_types": list(self.allowed_artifact_types),
            "allowed_ref_kinds": list(self.allowed_ref_kinds),
            "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
            "shared_read_only_refs": list(self.shared_read_only_refs),
            "writable_refs": list(self.writable_refs),
            "pinned_checksums": dict(self.pinned_checksums),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "policy_checksum": self.policy_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RefAccessPolicy":
        if not isinstance(value, Mapping):
            raise _ref_error("reference policy must be an object", code="REF_INVALID_POLICY")
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = sorted(set(value) - expected)
        missing = sorted(expected - set(value) - {"policy_checksum"})
        if unknown or missing:
            raise _ref_error("reference policy fields are invalid", code="REF_INVALID_POLICY", unknown=unknown, missing=missing)
        supplied = _checksum(value.get("policy_checksum"), "policy_checksum")
        payload = dict(value)
        payload.pop("policy_checksum", None)
        policy = cls(**payload)
        if supplied != policy.policy_checksum:
            raise _ref_error("reference policy checksum does not match its identity", code="REF_CHECKSUM_MISMATCH")
        return policy


@runtime_checkable
class RefResolutionPort(Protocol):
    """Trusted resolver for immutable reference descriptors."""

    def resolve(self, ref: str) -> RefDescriptor | None: ...


class InMemoryRefResolutionPort(RefResolutionPort):
    """Explicit test/local resolver; production composition must provide a durable port."""

    is_durable = False

    def __init__(self, descriptors: Sequence[RefDescriptor] = ()) -> None:
        self._lock = RLock()
        self._descriptors: dict[str, RefDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: RefDescriptor) -> None:
        if not isinstance(descriptor, RefDescriptor):
            raise TypeError("descriptor must be RefDescriptor")
        with self._lock:
            existing = self._descriptors.get(descriptor.ref)
            if existing is not None and existing.descriptor_checksum != descriptor.descriptor_checksum:
                raise _ref_error("reference descriptor identity conflict", code="REF_IDENTITY_CONFLICT", ref=descriptor.ref)
            self._descriptors[descriptor.ref] = descriptor

    def resolve(self, ref: str) -> RefDescriptor | None:
        with self._lock:
            return self._descriptors.get(ref)

    def resolve_namespace(self, namespace: str) -> RefDescriptor | None:
        with self._lock:
            matches = [item for item in self._descriptors.values() if item.namespace == namespace]
        if not matches:
            return None
        if len({item.descriptor_checksum for item in matches}) != 1:
            raise _ref_error("memory namespace resolves to conflicting descriptors", code="REF_IDENTITY_CONFLICT", namespace=namespace)
        return matches[0]


class RefAuthority:
    """Pure Harness gate shared by input, result, planning and memory refs."""

    schema_version = REF_DESCRIPTOR_SCHEMA

    def __init__(self, *, resolver: RefResolutionPort | None = None) -> None:
        if resolver is not None and not isinstance(resolver, RefResolutionPort):
            raise TypeError("resolver must implement RefResolutionPort")
        self._resolver = resolver

    @property
    def resolver(self) -> RefResolutionPort | None:
        return self._resolver

    @staticmethod
    def require_scope(policy: RefAccessPolicy, *, run_id: str, stage_id: str) -> None:
        if not isinstance(policy, RefAccessPolicy):
            raise _ref_error("reference authority requires a pinned policy", code="REF_POLICY_REQUIRED")
        if (policy.run_id, policy.stage_id) != (run_id, stage_id):
            raise _ref_error("reference policy is outside the current run or stage", code="REF_POLICY_SCOPE_MISMATCH")

    def authorize(
        self,
        descriptor: RefDescriptor | Mapping[str, Any],
        policy: RefAccessPolicy,
        *,
        requested_access: RefAccessMode | str = RefAccessMode.READ_ONLY,
        expected_kind: str | None = None,
        expected_artifact_type: str | None = None,
        expected_checksum: str | None = None,
        requester_owner_id: str | None = None,
    ) -> RefDescriptor:
        if not isinstance(policy, RefAccessPolicy):
            raise TypeError("policy must be RefAccessPolicy")
        if not isinstance(descriptor, RefDescriptor):
            descriptor = RefDescriptor.from_dict(descriptor)
        try:
            requested = RefAccessMode(requested_access)
        except (TypeError, ValueError) as exc:
            raise _ref_error("requested reference access is invalid", code="REF_INVALID_ACCESS") from exc
        if requester_owner_id is not None and identifier(requester_owner_id, "requester_owner_id") != policy.owner_id:
            raise _ref_error("requester owner is outside the pinned policy", owner_id=requester_owner_id)
        if descriptor.ref not in policy.allowed_refs:
            raise _ref_error("reference is not in the pinned allowlist", ref=descriptor.ref)
        if descriptor.ref_kind not in policy.allowed_ref_kinds:
            raise _ref_error("reference kind is not allowed by policy", ref=descriptor.ref, ref_kind=descriptor.ref_kind)
        if expected_kind is not None and descriptor.ref_kind != expected_kind:
            raise _ref_error("reference kind does not match the requested boundary", ref=descriptor.ref, expected_kind=expected_kind)
        if descriptor.artifact_type not in policy.allowed_artifact_types:
            raise _ref_error("reference artifact type is not allowed", ref=descriptor.ref, artifact_type=descriptor.artifact_type)
        if expected_artifact_type is not None and descriptor.artifact_type != expected_artifact_type:
            raise _ref_error("reference artifact type does not match the requested boundary", ref=descriptor.ref, expected_artifact_type=expected_artifact_type)
        if expected_checksum is not None and descriptor.source_checksum != _checksum(expected_checksum, "expected_checksum"):
            raise _ref_error("reference source checksum does not match expected evidence", ref=descriptor.ref, code="REF_CHECKSUM_MISMATCH")
        pinned = policy.pinned_checksums.get(descriptor.ref)
        if pinned is None:
            raise _ref_error("reference has no pinned source checksum", ref=descriptor.ref, code="REF_CHECKSUM_UNPINNED")
        if descriptor.source_checksum != pinned:
            raise _ref_error("reference source checksum differs from pinned evidence", ref=descriptor.ref, code="REF_CHECKSUM_MISMATCH")
        if descriptor.ref_kind == REF_KIND_MEMORY and descriptor.namespace not in policy.allowed_memory_namespaces:
            raise _ref_error("memory namespace is not allowed", ref=descriptor.ref, namespace=descriptor.namespace)
        cross_scope = (
            descriptor.run_id != policy.run_id
            or descriptor.stage_id != policy.stage_id
            or descriptor.tenant_id != policy.tenant_id
        )
        owner_mismatch = descriptor.owner_id != policy.owner_id
        explicitly_shared = descriptor.ref in policy.shared_read_only_refs
        if cross_scope or owner_mismatch:
            if not explicitly_shared or descriptor.scope is not RefScope.SHARED_READ_ONLY or requested is not RefAccessMode.READ_ONLY:
                raise _ref_error("reference is outside the pinned owner, tenant, run, or stage scope", ref=descriptor.ref)
        if requested is RefAccessMode.READ_WRITE:
            if descriptor.access_mode is not RefAccessMode.READ_WRITE or descriptor.ref not in policy.writable_refs:
                raise _ref_error("write access is not authorized for this reference", ref=descriptor.ref)
        if descriptor.scope is RefScope.SHARED_READ_ONLY and requested is not RefAccessMode.READ_ONLY:
            raise _ref_error("shared references are read-only", ref=descriptor.ref)
        return descriptor

    def authorize_ref(
        self,
        ref: str,
        policy: RefAccessPolicy,
        *,
        resolver: RefResolutionPort | None = None,
        descriptors: Mapping[str, RefDescriptor] | None = None,
        **kwargs: Any,
    ) -> RefDescriptor:
        normalized = reference(ref, "ref")
        descriptor = normalize_ref_descriptors(descriptors).get(normalized)
        source = self._resolver if resolver is None else resolver
        if descriptor is None:
            if source is None:
                raise _ref_error("reference resolver is unavailable", code="REF_RESOLVER_UNAVAILABLE", ref=normalized)
            if not isinstance(source, RefResolutionPort):
                raise TypeError("resolver must implement RefResolutionPort")
            descriptor = source.resolve(normalized)
        if descriptor is None:
            raise _ref_error("reference descriptor is unavailable", code="REF_UNRESOLVED", ref=normalized)
        if not isinstance(descriptor, RefDescriptor):
            raise _ref_error("reference resolver returned an invalid descriptor", code="REF_INVALID_DESCRIPTOR", ref=normalized)
        if descriptor.ref != normalized:
            raise _ref_error("reference resolver returned a different identity", code="REF_IDENTITY_CONFLICT", ref=normalized)
        return self.authorize(descriptor, policy, **kwargs)

    def authorize_many(
        self,
        refs: Sequence[str],
        policy: RefAccessPolicy,
        *,
        expected_kind: str,
        resolver: RefResolutionPort | None = None,
        **kwargs: Any,
    ) -> tuple[RefDescriptor, ...]:
        if expected_kind not in REF_KINDS:
            raise _ref_error("reference kind is unsupported", code="REF_INVALID_DESCRIPTOR")
        normalized = _reference_tuple(refs, "refs")
        return tuple(
            self.authorize_ref(ref, policy, resolver=resolver, expected_kind=expected_kind, **kwargs)
            for ref in normalized
        )

    def authorize_memory_namespace(
        self,
        descriptor: RefDescriptor | Mapping[str, Any],
        policy: RefAccessPolicy,
        *,
        requested_access: RefAccessMode | str = RefAccessMode.READ_ONLY,
    ) -> RefDescriptor:
        return self.authorize(
            descriptor,
            policy,
            requested_access=requested_access,
            expected_kind=REF_KIND_MEMORY,
            expected_artifact_type="memory_namespace",
        )

    def authorize_memory_namespace_ref(
        self,
        namespace: str,
        policy: RefAccessPolicy,
        *,
        resolver: RefResolutionPort | None = None,
        descriptors: Mapping[str, RefDescriptor] | None = None,
        requested_access: RefAccessMode | str = RefAccessMode.READ_ONLY,
    ) -> RefDescriptor:
        normalized = reference(namespace, "namespace")
        descriptor = normalize_ref_descriptors(descriptors).get(normalized)
        source = self._resolver if resolver is None else resolver
        if descriptor is None and source is not None:
            if not isinstance(source, RefResolutionPort):
                raise TypeError("resolver must implement RefResolutionPort")
            resolve_namespace = getattr(source, "resolve_namespace", None)
            if callable(resolve_namespace):
                descriptor = resolve_namespace(normalized)
            if descriptor is None:
                descriptor = source.resolve(normalized)
        if descriptor is None:
            raise _ref_error("memory namespace descriptor is unavailable", code="REF_UNRESOLVED", namespace=normalized)
        if not isinstance(descriptor, RefDescriptor):
            raise _ref_error("memory resolver returned an invalid descriptor", code="REF_INVALID_DESCRIPTOR")
        if normalized not in (descriptor.ref, descriptor.namespace):
            raise _ref_error("memory namespace descriptor identity does not match", code="REF_IDENTITY_CONFLICT", namespace=normalized)
        return self.authorize_memory_namespace(descriptor, policy, requested_access=requested_access)


def normalize_ref_descriptors(
    descriptors: Mapping[str, RefDescriptor] | None,
) -> Mapping[str, RefDescriptor]:
    if descriptors is None:
        return MappingProxyType({})
    if not isinstance(descriptors, Mapping):
        raise TypeError("ref_descriptors must be a mapping")
    normalized: dict[str, RefDescriptor] = {}
    for key, descriptor in descriptors.items():
        if not isinstance(descriptor, RefDescriptor):
            raise TypeError("ref_descriptors values must be RefDescriptor")
        if key not in (descriptor.ref, descriptor.namespace):
            raise _ref_error("reference descriptor alias does not match its identity", code="REF_IDENTITY_CONFLICT")
        for identity in (descriptor.ref, descriptor.namespace):
            if identity is None:
                continue
            existing = normalized.get(identity)
            if existing is not None and existing != descriptor:
                raise _ref_error("reference descriptor identity is ambiguous", code="REF_IDENTITY_CONFLICT", ref=identity)
            normalized[identity] = descriptor
    return MappingProxyType(dict(sorted(normalized.items())))


def validate_ref_configuration(
    authority: RefAuthority | None,
    policy: RefAccessPolicy | None,
    resolver: RefResolutionPort | None,
    descriptors: Mapping[str, RefDescriptor],
) -> None:
    if authority is None:
        if policy is not None or resolver is not None or descriptors:
            raise _ref_error("reference policy inputs require RefAuthority", code="REF_AUTHORITY_REQUIRED")
        return
    if not isinstance(authority, RefAuthority):
        raise TypeError("ref_authority must be RefAuthority")
    if not isinstance(policy, RefAccessPolicy):
        raise _ref_error("reference authority requires a pinned policy", code="REF_POLICY_REQUIRED")
    if resolver is not None and not isinstance(resolver, RefResolutionPort):
        raise TypeError("ref_resolution must implement RefResolutionPort")


__all__ = [
    "REF_ACCESS_READ_ONLY",
    "REF_ACCESS_READ_WRITE",
    "REF_DESCRIPTOR_SCHEMA",
    "REF_KIND_INPUT",
    "REF_KIND_MEMORY",
    "REF_KIND_PLANNING",
    "REF_KIND_RESULT",
    "REF_KINDS",
    "REF_POLICY_SCHEMA",
    "REF_SCOPE_PRIVATE",
    "REF_SCOPE_SHARED_READ_ONLY",
    "InMemoryRefResolutionPort",
    "RefAccessMode",
    "RefAccessPolicy",
    "RefAuthority",
    "RefDescriptor",
    "RefResolutionPort",
    "RefScope",
]
