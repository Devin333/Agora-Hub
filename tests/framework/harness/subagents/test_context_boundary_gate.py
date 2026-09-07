from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness import FakeSubAgentRuntime, SubAgentContextBoundaryGate, fake_subagent_spec
from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    RefAccessPolicy, RefAuthority, RefDescriptor, RefScope, InMemoryRefResolutionPort,
)
from framework.harness.subagents.context import SubAgentContextBuilder


class UnsafeEnvelope:
    def to_dict(self):
        return {"context_pack": {"dynamic_tail": {"sibling_private_notes": ["do not share"]}}}


def test_context_boundary_gate_rejects_sibling_private_notes() -> None:
    result = SubAgentContextBoundaryGate().evaluate(UnsafeEnvelope())  # type: ignore[arg-type]

    assert result.passed is False
    assert result.details["forbidden"] == ["sibling_private_notes"]


def test_fake_runtime_child_does_not_receive_parent_raw_messages() -> None:
    runtime = FakeSubAgentRuntime(fake_subagent_spec())
    invocation = runtime.build_invocation()
    result = runtime.invoke(invocation)

    assert result.status == "succeeded"
    context_payload = invocation.context_envelope.to_dict()["context_pack"]
    assert "parent_raw_messages" not in context_payload


@pytest.mark.parametrize("as_uri", [False, True])
def test_child_memory_uses_real_scope_and_child_namespace_intersection(as_uri) -> None:
    spec = fake_subagent_spec()
    invocation = FakeSubAgentRuntime(spec).build_invocation()
    namespace = spec.allowed_memory_namespaces[0]
    memory = RefDescriptor.memory(
        namespace=namespace, ref="memory://child-context",
        run_id=invocation.parent_run_id, stage_id=invocation.stage_id,
        tenant_id="tenant-1", owner_id=invocation.child_run_id,
        source_checksum=checksum_for({"memory": "child-context"}),
    )
    policy = RefAccessPolicy(
        policy_id="child.refs", version="1", run_id=invocation.parent_run_id,
        stage_id=invocation.stage_id, tenant_id="tenant-1",
        owner_id=invocation.child_run_id, allowed_refs=(memory.ref,),
        allowed_artifact_types=("memory_namespace",),
        allowed_memory_namespaces=(namespace,),
        pinned_checksums={memory.ref: memory.source_checksum},
    )
    arguments = dict(
        parent_run_id=invocation.parent_run_id,
        child_run_id=invocation.child_run_id,
        spec=spec,
        context_pack=invocation.context_envelope.context_pack,
        input_refs=(), memory_context_refs=(memory.ref if as_uri else namespace,),
        budget_snapshot=invocation.budget_snapshot,
        ref_authority=RefAuthority(resolver=InMemoryRefResolutionPort((memory,))),
        ref_policy=policy,
    )
    envelope = SubAgentContextBuilder().build(**arguments)
    assert envelope.memory_context_refs == arguments["memory_context_refs"]
    with pytest.raises(HarnessValidationError) as error:
        SubAgentContextBuilder().build(**{**arguments, "spec": replace(spec, allowed_memory_namespaces=("other",))})
    assert error.value.code == "REF_UNAUTHORIZED"
    with pytest.raises(HarnessValidationError) as error:
        SubAgentContextBuilder().build(**{**arguments, "ref_policy": replace(policy, owner_id="sibling-child")})
    assert error.value.code == "REF_UNAUTHORIZED"
    sibling_memory = replace(memory, owner_id="sibling-child")
    with pytest.raises(HarnessValidationError) as error:
        SubAgentContextBuilder().build(**{
            **arguments,
            "ref_authority": RefAuthority(resolver=InMemoryRefResolutionPort((sibling_memory,))),
        })
    assert error.value.code == "REF_UNAUTHORIZED"
    shared_memory = replace(sibling_memory, scope=RefScope.SHARED_READ_ONLY)
    shared_envelope = SubAgentContextBuilder().build(**{
        **arguments,
        "ref_authority": RefAuthority(resolver=InMemoryRefResolutionPort((shared_memory,))),
        "ref_policy": replace(policy, shared_read_only_refs=(memory.ref,)),
    })
    assert shared_envelope.memory_context_refs == envelope.memory_context_refs
