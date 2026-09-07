from __future__ import annotations

from dataclasses import replace

import pytest

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    REF_KIND_INPUT,
    REF_KIND_MEMORY,
    REF_KIND_RESULT,
    REF_SCOPE_SHARED_READ_ONLY,
    InMemoryRefResolutionPort,
    RefAccessMode,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    RefScope,
    normalize_ref_descriptors,
    validate_ref_configuration,
)


def _checksum(label: str) -> str:
    return checksum_for({"label": label})


def _descriptor(
    ref: str = "artifact://run-1/stage-1/document",
    *,
    run_id: str = "run-1",
    stage_id: str = "stage-1",
    tenant_id: str = "tenant-1",
    owner_id: str = "agent-parent",
    access_mode: RefAccessMode = RefAccessMode.READ_ONLY,
    artifact_type: str = "document",
    ref_kind: str = REF_KIND_INPUT,
    scope: RefScope = RefScope.PRIVATE,
    namespace: str | None = None,
) -> RefDescriptor:
    return RefDescriptor(
        ref=ref,
        run_id=run_id,
        stage_id=stage_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        access_mode=access_mode,
        artifact_type=artifact_type,
        source_checksum=_checksum(ref),
        ref_kind=ref_kind,
        namespace=namespace,
        scope=scope,
    )


def _policy(
    descriptor: RefDescriptor,
    *,
    owner_id: str = "agent-parent",
    shared: tuple[str, ...] = (),
    writable: tuple[str, ...] = (),
    allowed_types: tuple[str, ...] = (),
    namespaces: tuple[str, ...] = (),
) -> RefAccessPolicy:
    return RefAccessPolicy(
        policy_id="policy.refs",
        version="1",
        run_id="run-1",
        stage_id="stage-1",
        tenant_id="tenant-1",
        owner_id=owner_id,
        allowed_refs=(descriptor.ref,),
        allowed_artifact_types=allowed_types or (descriptor.artifact_type,),
        allowed_memory_namespaces=namespaces,
        shared_read_only_refs=shared,
        writable_refs=writable,
        pinned_checksums={descriptor.ref: descriptor.source_checksum},
    )


def test_descriptor_and_policy_round_trip_bind_canonical_checksums() -> None:
    descriptor = _descriptor()
    policy = _policy(descriptor)

    assert RefDescriptor.from_dict(descriptor.to_dict()) == descriptor
    assert RefAccessPolicy.from_dict(policy.to_dict()) == policy
    assert descriptor.descriptor_checksum.startswith("sha256:")
    assert policy.policy_checksum.startswith("sha256:")

    tampered_descriptor = descriptor.to_dict()
    tampered_descriptor["source_checksum"] = _checksum("different")
    with pytest.raises(HarnessValidationError, match="checksum"):
        RefDescriptor.from_dict(tampered_descriptor)

    tampered_policy = policy.to_dict()
    tampered_policy["tenant_id"] = "tenant-2"
    with pytest.raises(HarnessValidationError, match="checksum"):
        RefAccessPolicy.from_dict(tampered_policy)


def test_authority_allows_same_scope_read_and_pinned_write_only() -> None:
    descriptor = _descriptor(access_mode=RefAccessMode.READ_WRITE)
    policy = _policy(descriptor, writable=(descriptor.ref,))
    authority = RefAuthority()

    assert authority.authorize(descriptor, policy) is descriptor
    assert authority.authorize(descriptor, policy, requested_access=RefAccessMode.READ_WRITE) is descriptor

    with pytest.raises(HarnessValidationError) as exc_info:
        authority.authorize(
            descriptor,
            replace(policy, writable_refs=()),
            requested_access=RefAccessMode.READ_WRITE,
        )
    assert exc_info.value.code == "REF_UNAUTHORIZED"


def test_cross_scope_requires_explicit_read_only_share_and_never_write() -> None:
    descriptor = _descriptor(
        run_id="run-other",
        stage_id="stage-other",
        tenant_id="tenant-other",
        owner_id="agent-sibling",
        scope=RefScope.SHARED_READ_ONLY,
    )
    policy = _policy(descriptor, shared=(descriptor.ref,))
    authority = RefAuthority()

    assert authority.authorize(descriptor, policy) is descriptor
    with pytest.raises(HarnessValidationError) as exc_info:
        authority.authorize(descriptor, policy, requested_access=RefAccessMode.READ_WRITE)
    assert exc_info.value.code == "REF_UNAUTHORIZED"

    private = replace(descriptor, scope=RefScope.PRIVATE)
    with pytest.raises(HarnessValidationError) as exc_info:
        authority.authorize(private, policy)
    assert exc_info.value.code == "REF_UNAUTHORIZED"


def test_memory_namespace_and_kind_are_authorized_by_the_same_boundary() -> None:
    descriptor = RefDescriptor.memory(
        namespace="research.public",
        ref="memory://research/public/run-1",
        run_id="run-1",
        stage_id="stage-1",
        tenant_id="tenant-1",
        owner_id="agent-parent",
        source_checksum=_checksum("memory"),
    )
    policy = _policy(descriptor, namespaces=("research.public",))
    authority = RefAuthority()
    assert authority.authorize_memory_namespace(descriptor, policy) is descriptor

    with pytest.raises(HarnessValidationError) as exc_info:
        authority.authorize_memory_namespace(
            descriptor,
            replace(policy, allowed_memory_namespaces=()),
        )
    assert exc_info.value.code == "REF_UNAUTHORIZED"

    wrong_kind = replace(descriptor, ref_kind=REF_KIND_RESULT, namespace=None, artifact_type="result")
    with pytest.raises(HarnessValidationError):
        authority.authorize_memory_namespace(wrong_kind, policy)


def test_resolver_and_pinned_allowlist_fail_closed() -> None:
    descriptor = _descriptor()
    policy = _policy(descriptor)
    authority = RefAuthority(resolver=InMemoryRefResolutionPort((descriptor,)))
    assert authority.authorize_ref(descriptor.ref, policy, expected_kind=REF_KIND_INPUT) is descriptor

    with pytest.raises(HarnessValidationError) as exc_info:
        RefAuthority().authorize_ref(descriptor.ref, policy)
    assert exc_info.value.code == "REF_RESOLVER_UNAVAILABLE"

    result_descriptor = _descriptor(
        ref="artifact://run-1/stage-1/result",
        artifact_type="analysis",
        ref_kind=REF_KIND_RESULT,
    )
    with pytest.raises(HarnessValidationError) as exc_info:
        authority.authorize(result_descriptor, policy)
    assert exc_info.value.code == "REF_UNAUTHORIZED"


def test_memory_namespace_can_resolve_through_shared_resolver() -> None:
    descriptor = RefDescriptor.memory(
        namespace="research.public",
        ref="memory://research/public/run-1",
        run_id="run-1",
        stage_id="stage-1",
        tenant_id="tenant-1",
        owner_id="agent-parent",
        source_checksum=_checksum("memory-resolver"),
    )
    policy = _policy(descriptor, namespaces=("research.public",))
    resolver = InMemoryRefResolutionPort((descriptor,))
    authority = RefAuthority(resolver=resolver)

    assert authority.authorize_memory_namespace_ref(
        "research.public",
        policy,
    ) == descriptor


def test_policy_rejects_non_reference_allowlist_entries() -> None:
    descriptor = _descriptor()
    with pytest.raises(HarnessValidationError) as exc_info:
        RefAccessPolicy(
            policy_id="policy.refs",
            version="1",
            run_id="run-1",
            stage_id="stage-1",
            tenant_id="tenant-1",
            owner_id="agent-parent",
            allowed_refs=("bad ref",),
            pinned_checksums={"bad ref": descriptor.source_checksum},
        )
    assert exc_info.value.code == "invalid_task_plan_reference"


@pytest.mark.parametrize("field", ["run_id", "stage_id", "tenant_id", "owner_id"])
def test_each_private_scope_dimension_is_independently_enforced(field: str) -> None:
    descriptor = _descriptor()
    wrong_scope = replace(descriptor, **{field: "foreign-scope"})
    with pytest.raises(HarnessValidationError) as error:
        RefAuthority().authorize(wrong_scope, _policy(descriptor))
    assert error.value.code == "REF_UNAUTHORIZED"


def test_policy_checksum_mapping_cannot_change_after_pinning() -> None:
    descriptor = _descriptor()
    original = {descriptor.ref: descriptor.source_checksum}
    policy = replace(_policy(descriptor), pinned_checksums=original)
    original[descriptor.ref] = _checksum("changed")
    assert policy.pinned_checksums[descriptor.ref] == descriptor.source_checksum
    with pytest.raises(TypeError):
        policy.pinned_checksums[descriptor.ref] = _checksum("changed")
    assert RefAccessPolicy.from_dict(policy.to_dict()) == policy


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"allowed_artifact_types": ()}, "REF_UNAUTHORIZED"),
        ({"allowed_artifact_types": ("other",)}, "REF_UNAUTHORIZED"),
        ({"allowed_ref_kinds": (REF_KIND_RESULT,)}, "REF_UNAUTHORIZED"),
        ({"pinned_checksums": {}}, "REF_CHECKSUM_UNPINNED"),
    ],
)
def test_authority_has_no_implicit_grants(changes, code: str) -> None:
    descriptor = _descriptor()
    with pytest.raises(HarnessValidationError) as error:
        RefAuthority().authorize(descriptor, replace(_policy(descriptor), **changes))
    assert error.value.code == code


def test_current_source_and_requested_evidence_checksums_are_independent() -> None:
    descriptor = _descriptor()
    authority = RefAuthority()
    policy = _policy(descriptor)
    with pytest.raises(HarnessValidationError) as error:
        authority.authorize(replace(descriptor, source_checksum=_checksum("changed")), policy)
    assert error.value.code == "REF_CHECKSUM_MISMATCH"
    with pytest.raises(HarnessValidationError) as error:
        authority.authorize(descriptor, policy, expected_checksum=_checksum("different-payload"))
    assert error.value.code == "REF_CHECKSUM_MISMATCH"


def test_descriptor_registry_is_frozen_and_rejects_aliases_and_conflicts() -> None:
    descriptor = _descriptor()
    source = {descriptor.ref: descriptor}
    snapshot = normalize_ref_descriptors(source)
    source.clear()
    assert snapshot[descriptor.ref] == descriptor
    with pytest.raises(TypeError):
        snapshot[descriptor.ref] = replace(descriptor, owner_id="sibling")
    with pytest.raises(HarnessValidationError) as error:
        normalize_ref_descriptors({"permitted-alias": descriptor})
    assert error.value.code == "REF_IDENTITY_CONFLICT"
    memory = RefDescriptor.memory(
        namespace="research.public", ref="memory://first", run_id="run-1",
        stage_id="stage-1", tenant_id="tenant-1", owner_id="agent-parent",
        source_checksum=_checksum("memory"),
    )
    with pytest.raises(HarnessValidationError) as error:
        normalize_ref_descriptors({memory.ref: memory, "memory://second": replace(memory, ref="memory://second")})
    assert error.value.code == "REF_IDENTITY_CONFLICT"


@pytest.mark.parametrize("as_uri", [False, True])
def test_memory_resolution_preserves_exact_namespace_or_uri_identity(as_uri: bool) -> None:
    memory = RefDescriptor.memory(
        namespace="research.public", ref="memory://first", run_id="run-1",
        stage_id="stage-1", tenant_id="tenant-1", owner_id="agent-parent",
        source_checksum=_checksum("memory"),
    )
    authority = RefAuthority(resolver=InMemoryRefResolutionPort((memory,)))
    assert authority.authorize_memory_namespace_ref(
        memory.ref if as_uri else memory.namespace,
        _policy(memory, namespaces=(memory.namespace,)),
    ) == memory


@pytest.mark.parametrize("value", ["not-a-descriptor", {}, 42])
def test_untrusted_resolver_output_is_rejected(value) -> None:
    class Resolver:
        def resolve(self, ref):
            return value

    descriptor = _descriptor()
    with pytest.raises(HarnessValidationError) as error:
        RefAuthority(resolver=Resolver()).authorize_ref(descriptor.ref, _policy(descriptor))
    assert error.value.code == "REF_INVALID_DESCRIPTOR"


def test_configuration_requires_authority_and_matching_execution_scope() -> None:
    descriptor = _descriptor()
    policy = _policy(descriptor)
    with pytest.raises(HarnessValidationError) as error:
        validate_ref_configuration(None, policy, None, {})
    assert error.value.code == "REF_AUTHORITY_REQUIRED"
    with pytest.raises(HarnessValidationError) as error:
        validate_ref_configuration(RefAuthority(), None, None, {})
    assert error.value.code == "REF_POLICY_REQUIRED"
    with pytest.raises(HarnessValidationError) as error:
        RefAuthority.require_scope(policy, run_id="foreign-run", stage_id=policy.stage_id)
    assert error.value.code == "REF_POLICY_SCOPE_MISMATCH"
