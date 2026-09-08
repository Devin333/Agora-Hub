"""Immutable, content-addressed memory revisions and metadata-only storage ports.

The revision identifies the complete serialized namespace, including its trusted
tenant, owner and sharing scope. A record's caller-supplied ``version`` is data;
it never determines namespace authority. Publication does not promote memory or
change an active namespace pointer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Protocol, runtime_checkable

from framework.memory.models import MemoryRecord
from framework.memory.policy import MemoryPolicy
from framework.memory.exceptions import MemoryPolicyDenied


MEMORY_NAMESPACE_SCHEMA = "newsroom.memory-namespace-revision/v1"
MEMORY_NAMESPACE_DESCRIPTOR_SCHEMA = "newsroom.memory-namespace-descriptor/v1"
_REF_PREFIX = "memory-namespace://"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*\Z")


class MemoryNamespaceError(ValueError):
    def __init__(self, message: str, *, code: str = "MEMORY_NAMESPACE_INVALID") -> None:
        super().__init__(message)
        self.code = code


def namespace_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def namespace_checksum(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) > 512 or _IDENTITY.fullmatch(value) is None:
        raise MemoryNamespaceError(f"{name} must be a bounded identity")
    return value


def namespace_revision(ref: str) -> str:
    if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
        raise MemoryNamespaceError("an exact memory namespace reference is required")
    revision = ref[len(_REF_PREFIX):]
    if _DIGEST.fullmatch(revision) is None:
        raise MemoryNamespaceError("memory namespace revision must be a full sha256 digest")
    return revision


@dataclass(frozen=True, slots=True)
class MemoryNamespaceDescriptor:
    namespace: str
    tenant_id: str
    owner_id: str
    shared_read_only: bool
    revision: str
    record_count: int
    payload_size_bytes: int
    schema_version: str = MEMORY_NAMESPACE_DESCRIPTOR_SCHEMA
    descriptor_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != MEMORY_NAMESPACE_DESCRIPTOR_SCHEMA:
            raise MemoryNamespaceError("unsupported memory namespace descriptor schema")
        for name in ("namespace", "tenant_id", "owner_id"):
            _identity(getattr(self, name), name)
        if type(self.shared_read_only) is not bool:
            raise MemoryNamespaceError("shared_read_only must be explicit boolean policy")
        if not isinstance(self.revision, str) or _DIGEST.fullmatch(self.revision) is None:
            raise MemoryNamespaceError("memory namespace revision must be a full sha256 digest")
        for name, minimum in (("record_count", 0), ("payload_size_bytes", 1)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise MemoryNamespaceError(f"{name} is invalid")
        object.__setattr__(self, "descriptor_checksum", namespace_checksum(namespace_bytes(self.checksum_projection())))

    @property
    def exact_ref(self) -> str:
        return _REF_PREFIX + self.revision

    @property
    def source_checksum(self) -> str:
        return "sha256:" + self.revision

    def checksum_projection(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "descriptor_checksum"}

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "descriptor_checksum": self.descriptor_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MemoryNamespaceDescriptor:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise MemoryNamespaceError("memory namespace descriptor fields are invalid")
        payload = dict(value)
        supplied = payload.pop("descriptor_checksum")
        result = cls(**payload)
        if supplied != result.descriptor_checksum:
            raise MemoryNamespaceError("memory namespace metadata checksum mismatch", code="MEMORY_NAMESPACE_CORRUPT")
        return result


@dataclass(frozen=True, slots=True)
class MemoryNamespaceRevision:
    """Canonical immutable bytes; mutable MemoryRecord instances never escape storage."""

    descriptor: MemoryNamespaceDescriptor
    payload: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, MemoryNamespaceDescriptor) or type(self.payload) is not bytes:
            raise MemoryNamespaceError("namespace revision requires metadata and immutable bytes")
        if (
            namespace_checksum(self.payload) != self.descriptor.source_checksum
            or len(self.payload) != self.descriptor.payload_size_bytes
        ):
            raise MemoryNamespaceError("memory namespace payload checksum mismatch", code="MEMORY_NAMESPACE_CORRUPT")
        try:
            document = json.loads(self.payload)
            if not isinstance(document, dict) or namespace_bytes(document) != self.payload:
                raise ValueError("noncanonical document")
            expected = {"schema_version", "namespace", "tenant_id", "owner_id", "shared_read_only", "records"}
            if set(document) != expected or document["schema_version"] != MEMORY_NAMESPACE_SCHEMA:
                raise ValueError("invalid document schema")
            for name in ("namespace", "tenant_id", "owner_id", "shared_read_only"):
                if (
                    document[name] != getattr(self.descriptor, name)
                    or type(document[name]) is not type(getattr(self.descriptor, name))
                ):
                    raise ValueError("scope mismatch")
            records = document["records"]
            if not isinstance(records, list) or len(records) != self.descriptor.record_count:
                raise ValueError("invalid records")
            ids = []
            for raw in records:
                record = MemoryRecord.from_dict(raw)
                if (
                    namespace_bytes(record.to_dict()) != namespace_bytes(raw)
                    or record.namespace != self.descriptor.namespace
                    or record.tenant_id != self.descriptor.tenant_id
                    or record.actor != self.descriptor.owner_id
                ):
                    raise ValueError("record scope or canonical fields mismatch")
                ids.append(record.memory_id)
            if ids != sorted(set(ids)):
                raise ValueError("record ids must be unique and ordered")
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise MemoryNamespaceError("memory namespace payload is invalid", code="MEMORY_NAMESPACE_CORRUPT") from exc

    @classmethod
    def create(
        cls,
        *,
        namespace: str,
        tenant_id: str,
        owner_id: str,
        shared_read_only: bool,
        records: Sequence[MemoryRecord],
    ) -> MemoryNamespaceRevision:
        if (
            isinstance(records, (str, bytes))
            or not isinstance(records, Sequence)
            or any(not isinstance(item, MemoryRecord) for item in records)
        ):
            raise MemoryNamespaceError("namespace records must be MemoryRecord instances")
        payload = namespace_bytes({
            "schema_version": MEMORY_NAMESPACE_SCHEMA, "namespace": namespace,
            "tenant_id": tenant_id, "owner_id": owner_id, "shared_read_only": shared_read_only,
            "records": [item.to_dict() for item in sorted(records, key=lambda item: item.memory_id)],
        })
        descriptor = MemoryNamespaceDescriptor(
            namespace=namespace, tenant_id=tenant_id, owner_id=owner_id,
            shared_read_only=shared_read_only, revision=hashlib.sha256(payload).hexdigest(),
            record_count=len(records), payload_size_bytes=len(payload),
        )
        return cls(descriptor, payload)

    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(MemoryRecord.from_dict(raw) for raw in json.loads(self.payload)["records"])


@runtime_checkable
class MemoryNamespaceDescriptorPort(Protocol):
    def describe(self, ref: str) -> MemoryNamespaceDescriptor | None: ...


@runtime_checkable
class MemoryNamespaceStorePort(MemoryNamespaceDescriptorPort, Protocol):
    def commit(self, revision: MemoryNamespaceRevision) -> MemoryNamespaceDescriptor: ...
    def read(self, ref: str) -> MemoryNamespaceRevision: ...


class MemoryNamespacePublisher:
    """Trusted composition supplies scope; every record must pass existing write policy.

    This stores a read-only revision, with no latest alias or active-memory
    promotion. It is deliberately not an Agent tool or ordinary recall adapter.
    """

    def __init__(
        self,
        store: MemoryNamespaceStorePort,
        *,
        namespace: str,
        tenant_id: str,
        owner_id: str,
        shared_read_only: bool,
        policy: MemoryPolicy,
    ) -> None:
        if not isinstance(store, MemoryNamespaceStorePort) or not isinstance(policy, MemoryPolicy):
            raise TypeError("namespace publication requires a store and MemoryPolicy")
        self.store = store
        self.namespace = _identity(namespace, "namespace")
        self.tenant_id = _identity(tenant_id, "tenant_id")
        self.owner_id = _identity(owner_id, "owner_id")
        if type(shared_read_only) is not bool:
            raise MemoryNamespaceError("sharing policy must be explicit")
        self.shared_read_only = shared_read_only
        self.policy = policy

    def publish(self, records: Sequence[MemoryRecord]) -> MemoryNamespaceDescriptor:
        if not self.policy.allow_write:
            raise MemoryPolicyDenied("memory namespace publication is disabled by policy")
        revision = MemoryNamespaceRevision.create(
            namespace=self.namespace, tenant_id=self.tenant_id, owner_id=self.owner_id,
            shared_read_only=self.shared_read_only, records=records,
        )
        for record in revision.records():
            self.policy.validate_write(record)
        stored = self.store.commit(revision)
        if stored != revision.descriptor or self.store.describe(stored.exact_ref) != stored:
            raise MemoryNamespaceError("namespace store returned different metadata", code="MEMORY_NAMESPACE_CORRUPT")
        return stored
