from __future__ import annotations

from collections.abc import Mapping

from framework.harness.context.models import ContextEnvelope
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.policy import HarnessBudgetSnapshot
from framework.harness.ref_authority import (
    REF_KIND_INPUT,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    RefResolutionPort,
    normalize_ref_descriptors,
    validate_ref_configuration,
)
from framework.harness.subagents.models import SubAgentContextEnvelope, SubAgentSpec
from framework.harness.subagents.policy import SubAgentToolPolicy


class SubAgentContextBuilder:
    def build(
        self,
        *,
        parent_run_id: str,
        child_run_id: str,
        spec: SubAgentSpec,
        context_pack: ContextEnvelope,
        input_refs: tuple[str, ...],
        memory_context_refs: tuple[str, ...],
        budget_snapshot: HarnessBudgetSnapshot,
        ref_authority: RefAuthority | None = None,
        ref_policy: RefAccessPolicy | None = None,
        ref_resolution: RefResolutionPort | None = None,
        ref_descriptors: Mapping[str, RefDescriptor] | None = None,
    ) -> SubAgentContextEnvelope:
        descriptors = normalize_ref_descriptors(ref_descriptors)
        validate_ref_configuration(ref_authority, ref_policy, ref_resolution, descriptors)
        if ref_authority is not None:
            graph_identity = context_pack.graph_identity
            if graph_identity is None or graph_identity.run_id != parent_run_id:
                raise HarnessValidationError("SubAgent reference scope requires its parent Graph identity", code="REF_POLICY_SCOPE_MISMATCH")
            ref_authority.require_scope(ref_policy, run_id=parent_run_id, stage_id=graph_identity.stage_id)
            if ref_policy.owner_id != child_run_id:
                raise HarnessValidationError("SubAgent reference policy must identify the child owner", code="REF_UNAUTHORIZED")
            ref_authority.authorize_many(
                input_refs,
                ref_policy,
                expected_kind=REF_KIND_INPUT,
                resolver=ref_resolution,
                descriptors=descriptors,
            )
            for ref in memory_context_refs:
                descriptor = ref_authority.authorize_memory_namespace_ref(
                    ref,
                    ref_policy,
                    resolver=ref_resolution,
                    descriptors=descriptors,
                )
                if descriptor.namespace not in spec.allowed_memory_namespaces:
                    raise HarnessValidationError(
                        "SubAgent memory reference is outside its declared namespace allowlist",
                        code="REF_UNAUTHORIZED",
                        details={"namespace": descriptor.namespace},
                    )
        tool_policy = SubAgentToolPolicy(
            subagent_id=spec.subagent_id,
            allowed_tools=spec.allowed_tools,
            policy_ref=f"tool-policy://{child_run_id}",
        )
        return SubAgentContextEnvelope(
            child_run_id=child_run_id,
            parent_run_id=parent_run_id,
            subagent_id=spec.subagent_id,
            role=spec.role,
            allowed_input_refs=input_refs,
            context_pack=context_pack,
            memory_context_refs=memory_context_refs,
            tool_policy_ref=tool_policy.policy_ref,
            budget_snapshot=budget_snapshot,
            redaction_report={"removed_private_fields": []},
        )


__all__ = ["SubAgentContextBuilder"]
