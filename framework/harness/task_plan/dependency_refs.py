"""Resolve dependency inputs from accepted TaskPlan results before payload IO."""

from __future__ import annotations

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_dependency_binding import DependencyResultBinding
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.task_plan.canonical import task_output_reference_producer
from framework.harness.task_plan.models import ResolvedTaskSpec, TaskLifecycle, ValidatedTaskPlan
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.task_plan.store import TaskPlanStorePort
from framework.harness.task_plan.verification import authorized_task_result_store
from framework.shared.graph_identity import GraphExecutionIdentity


def _denied(message: str, code: str = "REF_DEPENDENCY_RESULT_INVALID") -> HarnessValidationError:
    return HarnessValidationError(message, code=code)


class AcceptedDependencyResultResolver:
    def __init__(self, *, store: TaskPlanStorePort, authority: HarnessResultRefAuthority, policy: TaskPlanPolicy) -> None:
        if not isinstance(store, TaskPlanStorePort) or not isinstance(policy, TaskPlanPolicy):
            raise TypeError("dependency resolution requires the accepted plan store and pinned policy")
        if not isinstance(authority, HarnessResultRefAuthority) or not authority.is_durable:
            raise TypeError("dependency resolution requires durable result authority")
        self.store = store
        self.authority = authority
        self.policy = policy

    def resolve(
        self, *, plan: ValidatedTaskPlan, task: ResolvedTaskSpec,
        execution_identity: GraphExecutionIdentity,
    ) -> tuple[DependencyResultBinding, ...]:
        if not self.authority.is_durable:
            raise _denied("dependency result authority is no longer durable", "REF_SNAPSHOT_MISSING")
        if (
            plan.policy_checksum != self.policy.policy_checksum
            or plan.policy_ref != self.policy.exact_ref
            or self.store.plan(plan.run_id, plan.stage_id) != plan
            or next((item for item in plan.tasks if item.task_id == task.task_id), None) != task
        ):
            raise _denied("dependency consumer differs from its current accepted plan")
        known = tuple(item.task_id for item in plan.tasks)
        requested = tuple((ref, task_output_reference_producer(ref, known)) for ref in task.task.input_refs)
        requested = tuple((ref, producer) for ref, producer in requested if producer is not None)
        if not requested:
            return ()
        before = self.store.load_projection(plan.run_id, plan.stage_id)
        if not before.matches_plan_identity(plan):
            raise _denied("dependency projection differs from the current plan")
        accepted = self.store.results_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version)
        bindings = []
        for logical_ref, producer_id in requested:
            if producer_id not in task.task.depends_on or producer_id == task.task_id:
                raise _denied("dependency output is not an accepted predecessor")
            producer = next((item for item in plan.tasks if item.task_id == producer_id), None)
            state = next((item for item in before.tasks if item.task_id == producer_id), None)
            records = tuple(item for item in accepted if item.task_id == producer_id)
            if (
                producer is None or state is None or state.status is not TaskLifecycle.SUCCEEDED
                or state.result is None or len(records) != 1
                or producer.output_role not in self.policy.shared_dependency_output_roles
                or state.task_definition_checksum != producer.task_definition_checksum
            ):
                raise _denied("predecessor has no policy-approved accepted output", "REF_UNAUTHORIZED")
            record = records[0]
            original = self.store.plan(plan.run_id, plan.stage_id, record.plan_version)
            original_task = next((item for item in original.tasks if item.task_id == producer_id), None) if original else None
            if (
                original is None or original_task != producer
                or original.plan_id != record.plan_id or record.plan_version > plan.version
                or original.policy_checksum != plan.policy_checksum
                or record.status is not TaskLifecycle.SUCCEEDED
                or record.attempt != state.attempts
                or record.task_checksum != producer.task_definition_checksum
                or record.binding_checksum != producer.binding_checksum
                or record.worker_ref != producer.worker_ref
                or record.output_schema_ref != producer.task.output_contract.schema_ref
                or record.output_roles != (producer.output_role,)
                or record.result_ref != record.subagent_output_ref
                or record.result_checksum != state.result.result_checksum
                or record.result_ref != state.result.result_ref
                or state.result.output_role != producer.output_role
                or state.result.output_schema_ref != record.output_schema_ref
            ):
                raise _denied("predecessor result differs from its accepted projection or original plan")
            instance = task_instance_for_attempt(
                original, producer_id, record.attempt, task_instance_id=record.task_instance_id,
            )
            bound = authorized_task_result_store(
                self.authority, plan=original, task=original_task, instance=instance,
                execution_identity=execution_identity,
            )
            evidence = self.authority.result_grant(bound.identity, allow_registration=False)
            if evidence is None:
                raise _denied("predecessor result grant is missing", "REF_SNAPSHOT_MISSING")
            grant, metadata = evidence
            receipt = metadata.receipt
            if (
                receipt.output_ref != record.result_ref or receipt.output_checksum != record.subagent_output_checksum
                or receipt.transcript_ref != record.transcript_ref or receipt.transcript_checksum != record.transcript_checksum
            ):
                raise _denied("predecessor receipt differs from accepted result", "REF_CHECKSUM_MISMATCH")
            bound.authorize_artifacts(record.output_refs, include_materialized=True)
            descriptor = next((item for item in grant.descriptors if item.ref == record.result_ref), None)
            if descriptor is None:
                raise _denied("accepted output has no source descriptor", "REF_SNAPSHOT_MISSING")
            bindings.append(DependencyResultBinding(
                logical_ref=logical_ref, producer_attempt=bound.identity,
                source_snapshot_ref=grant.snapshot_ref, accepted_result_checksum=record.result_checksum,
                output_role=producer.output_role, descriptor=descriptor,
            ))
        after = self.store.load_projection(plan.run_id, plan.stage_id)
        producer_ids = {producer for _, producer in requested}
        if (
            self.store.plan(plan.run_id, plan.stage_id) != plan
            or not after.matches_plan_identity(plan)
            or tuple(item for item in after.tasks if item.task_id in producer_ids)
            != tuple(item for item in before.tasks if item.task_id in producer_ids)
        ):
            raise _denied("accepted dependency projection changed during resolution")
        return tuple(bindings)
