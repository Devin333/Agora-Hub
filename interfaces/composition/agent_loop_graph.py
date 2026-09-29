from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from framework.agent.loop.runner import AgentRunner
from framework.harness.artifacts import RunBoundArtifactPort
from framework.harness.agent_loop import (
    AgentOrchestrationBinding,
    AgentOrchestrationCompositionState,
    AgentOrchestrationDispatch,
    AgentOrchestrationTaskProfile,
    AgentLoopGraphActivityBindingBundle,
    AgentLoopGraphArtifactRecorder,
    HarnessAgentOrchestrationRuntime,
    build_agent_loop_graph_activity_binding_bundle,
)
from framework.harness.control_plane import (
    HarnessControlPlane,
    HarnessRunResult,
    HarnessRunSpec,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.activity_execution import (
    HarnessGraphActivityExecutionCommitPort,
)
from framework.harness.control_plane.node_output import HarnessNodeOutputResourcePort
from framework.harness.graph import HarnessContractReference
from framework.harness.ports import HarnessTransitionPort
from framework.harness.runtime import (
    HarnessGraphPhysicalActivityDispatcher,
    HarnessGraphPhysicalActivityExecutor,
)
from framework.harness.side_effects import (
    HarnessSideEffectRegistry,
    HarnessSideEffectStorePort,
)
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.agent_loop.child_executor import HarnessSubAgentTaskExecutor
from framework.harness.ref_authority import RefResolutionPort
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_planning import HarnessPlanningRefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefAuthoritySnapshotStorePort
from framework.harness.task_plan.capability import TaskCapabilityRegistry
from framework.harness.task_plan.checkpoint import TaskPlanCheckpointStorePort
from framework.harness.task_plan.durable_store import DurableTaskPlanStore
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from framework.harness.task_plan.planning_observation import (
    HarnessPlanningObservationService,
    PlanningObservationPort,
    PlanningObservationPolicy,
)
from framework.harness.task_plan.policy import TaskPlanPolicy, TaskPlanPolicyRegistry
from framework.harness.task_plan.ports import (
    PlanCandidateBuilderPort,
    TaskPlanResultVerifierPort,
)
from framework.harness.task_plan.stage import TaskPlanStageRunner
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.shared.attempts import AttemptSupervisor
from framework.shared.time import utc_now
from framework.agent.models import AgentSpec
from framework.execution_environment.composition import RuntimeExecutionComposition
from framework.execution_environment.errors import RuntimeCompositionDriftError
from framework.tool import ToolExecutor, ToolRegistry
from infrastructure.storage.harness.planning_observation import FilesystemPlanningObservationStore
from interfaces.services.agent_loop_graph_service import (
    AgentLoopGraphApplicationService,
)


@dataclass(frozen=True, slots=True)
class AgentLoopOrchestrationFeature:
    """Explicit rollout selection for the generic Harness delegation port.

    The value is supplied by the application settings/composition layer.  It
    intentionally does not let a worker or an AgentLoop action turn the
    feature on for itself.
    """

    enabled: bool = False
    rollout_scope: str = "generic"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be boolean")
        if self.rollout_scope not in {"generic", "research_dynamic"}:
            raise ValueError(
                "rollout_scope must be 'generic' or 'research_dynamic'"
            )

    def enabled_for(self, scope: str) -> bool:
        """Return whether this immutable rollout decision applies to ``scope``."""

        if scope not in {"generic", "research_dynamic"}:
            raise ValueError("scope must be 'generic' or 'research_dynamic'")
        return self.enabled and self.rollout_scope == scope


def build_agent_loop_planning_observation_service(
    *,
    policy: TaskPlanPolicy,
    executor: ToolExecutor,
    registry: ToolRegistry,
    receipt_path: str | Path,
    input_snapshot: RefAuthoritySnapshot,
    snapshot_store: RefAuthoritySnapshotStorePort,
    planner_turn: int,
    artifact_resolution: RefResolutionPort | None = None,
    allowed_artifact_types: tuple[str, ...] = (),
) -> HarnessPlanningObservationService:
    """Build the durable, Harness-owned planning tool boundary.

    The caller supplies the admitted Graph input grant and bounded planner
    ordinal. Metadata and receipts live under ``receipt_path``; authority is
    committed to the canonical run stream before any receipt is consumed.
    """

    if not isinstance(policy, TaskPlanPolicy):
        raise TypeError("policy must be TaskPlanPolicy")
    if not isinstance(executor, ToolExecutor):
        raise TypeError("executor must be ToolExecutor")
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be ToolRegistry")
    if getattr(snapshot_store, "is_durable", False) is not True:
        raise ValueError("planning observations require durable reference authority")
    if artifact_resolution is not None and getattr(artifact_resolution, "is_durable", False) is not True:
        raise ValueError("planning artifact metadata requires a durable resolver")
    if isinstance(planner_turn, bool) or not isinstance(planner_turn, int) or not 1 <= planner_turn <= policy.max_plan_build_turns:
        raise ValueError("planner_turn exceeds the admitted planning policy")
    receipt_store = FilesystemPlanningObservationStore(receipt_path, input_snapshot=input_snapshot)
    authority = HarnessPlanningRefAuthority(
        snapshot_store, receipt_store=receipt_store, input_snapshot=input_snapshot,
        planner_turn=planner_turn, artifact_resolution=artifact_resolution,
        allowed_artifact_types=allowed_artifact_types,
    )
    return HarnessPlanningObservationService(
        executor=executor,
        registry=registry,
        store=receipt_store,
        policy=PlanningObservationPolicy.from_task_plan_policy(policy, registry),
        planning_ref_authority=authority,
    )


def build_agent_loop_harness_orchestration_runtime(
    *,
    stage_binding: TaskPlanStageBinding,
    policy_registry: TaskPlanPolicyRegistry,
    capability_registry: TaskCapabilityRegistry,
    store: DurableTaskPlanStore,
    child_supervisor: ChildAgentSupervisor,
    candidate_builder: Any,
    worker_executor: HarnessSubAgentTaskExecutor,
    task_profiles: tuple[AgentOrchestrationTaskProfile, ...],
    result_verifier: Any | None = None,
    planning_observation_port: PlanningObservationPort | None = None,
    metrics_sink: Any | None = None,
    checkpoint_store: TaskPlanCheckpointStorePort,
    ref_admission_service: HarnessRefAdmissionService | None = None,
) -> HarnessAgentOrchestrationRuntime:
    """Compose the real AgentLoop fan-out runtime from durable dependencies.

    This is the only production constructor for the generic delegate-batch
    path.  It deliberately has no in-memory fallback and always routes child
    execution through the supplied lifecycle supervisor.
    """

    if not isinstance(store, DurableTaskPlanStore):
        raise TypeError("store must be DurableTaskPlanStore")
    if not isinstance(stage_binding, TaskPlanStageBinding):
        raise TypeError("stage_binding must be TaskPlanStageBinding")
    if not isinstance(policy_registry, TaskPlanPolicyRegistry):
        raise TypeError("policy_registry must be TaskPlanPolicyRegistry")
    if not isinstance(capability_registry, TaskCapabilityRegistry):
        raise TypeError("capability_registry must be TaskCapabilityRegistry")
    if not isinstance(child_supervisor, ChildAgentSupervisor):
        raise TypeError("child_supervisor must be ChildAgentSupervisor")
    child_supervisor.require_durable_owner()
    if not isinstance(checkpoint_store, TaskPlanCheckpointStorePort):
        raise TypeError("checkpoint_store must implement TaskPlanCheckpointStorePort")
    if getattr(checkpoint_store, "is_durable", False) is not True:
        raise ValueError(
            "production AgentLoop orchestration requires a durable checkpoint store"
        )
    if not callable(worker_executor):
        raise TypeError("worker_executor must be callable")
    if not isinstance(candidate_builder, PlanCandidateBuilderPort):
        raise TypeError(
            "candidate_builder must implement PlanCandidateBuilderPort"
        )
    # Resolve the exact stage policy before constructing any executable
    # runtime.  This makes missing or stale worker bindings a composition
    # error instead of a late child-admission failure.
    policy = policy_registry.resolve(
        stage_binding.policy_ref,
        stage_id=stage_binding.stage_id,
    )
    profiles = tuple(task_profiles)
    if not profiles:
        raise ValueError("task_profiles must not be empty")
    for profile in profiles:
        if not isinstance(profile, AgentOrchestrationTaskProfile):
            raise TypeError(
                "task_profiles must contain AgentOrchestrationTaskProfile values"
            )
        try:
            capability_registry.resolve(profile.capability_hint, policy)
        except HarnessValidationError as exc:
            raise ValueError(
                "worker capability binding is not available for the production "
                f"AgentLoop orchestration runtime: {exc}"
            ) from exc

    if not isinstance(result_verifier, TaskPlanResultVerifierPort):
        raise TypeError(
            "result_verifier must implement TaskPlanResultVerifierPort"
        )
    if getattr(result_verifier, "gate_artifact_writer", None) is not store:
        raise ValueError(
            "result_verifier must use the configured TaskPlan gate artifact owner"
        )
    if (
        planning_observation_port is not None or policy.max_planning_tool_calls > 0
    ) and not isinstance(planning_observation_port, PlanningObservationPort):
        raise TypeError(
            "planning_observation_port must implement PlanningObservationPort"
        )
    planning_authority = getattr(planning_observation_port, "planning_ref_authority", None)
    if planning_observation_port is not None and (
        not isinstance(planning_observation_port, HarnessPlanningObservationService)
        or not isinstance(planning_authority, HarnessPlanningRefAuthority)
        or not planning_observation_port.is_durable
        or planning_authority.input_snapshot.stage_binding_checksum != stage_binding.binding_checksum
        or planning_authority.input_snapshot.task_policy_checksum != policy.policy_checksum
    ):
        raise ValueError("planning_observation_port requires durable execution-bound reference authority")
    if planning_authority is not None and (
        ref_admission_service is None or planning_authority.store is not ref_admission_service.store
    ):
        raise ValueError("planning and parent input admission must share the canonical snapshot store")
    if not isinstance(worker_executor, HarnessSubAgentTaskExecutor):
        raise TypeError("worker_executor must be HarnessSubAgentTaskExecutor")

    coordinator = ParallelAgentCoordinator(
        max_workers=child_supervisor.capacity,
        child_supervisor=child_supervisor,
    )
    runner = TaskPlanStageRunner(
        candidate_builder=candidate_builder,
        capability_registry=capability_registry,
        store=store,
        result_verifier=result_verifier,
        worker_executor=worker_executor,
        worker_result_recovery=worker_executor.recover,
        parallel_coordinator=coordinator,
        child_supervisor_capacity=child_supervisor.capacity,
        planning_observation_port=planning_observation_port,
        metrics_sink=metrics_sink,
        checkpoint_store=checkpoint_store,
    )
    return HarnessAgentOrchestrationRuntime(
        stage_binding=stage_binding,
        policy_registry=policy_registry,
        capability_registry=capability_registry,
        store=store,
        stage_runner=runner,
        child_supervisor=child_supervisor,
        task_profiles=task_profiles,
        require_durable_store=True,
        ref_admission_service=ref_admission_service,
    )


class AgentLoopGraphRuntimeComposition:
    """Production composition root for one Graph-bound AgentLoop worker.

    It owns only exact runtime registration and physical activity dispatch. The
    Harness control plane remains the sole owner of Graph routing, durable
    wait registration/resume, terminal state, and publication decisions.
    """

    def __init__(
        self,
        *,
        agent_runner: AgentRunner,
        agent: AgentSpec,
        artifact_port: RunBoundArtifactPort,
        node_output_resource: HarnessNodeOutputResourcePort,
        event_port: HarnessTransitionPort,
        worker_ref: HarnessContractReference,
        activity_ref: HarnessContractReference,
        side_effect_registry: HarnessSideEffectRegistry | None = None,
        side_effect_store: HarnessSideEffectStorePort | None = None,
        runtime_execution_composition: RuntimeExecutionComposition | None = None,
        orchestration_runtime: HarnessAgentOrchestrationRuntime | None = None,
        orchestration_dispatch: AgentOrchestrationDispatch | None = None,
        orchestration_feature_enabled: bool = False,
        orchestration_feature: AgentLoopOrchestrationFeature | None = None,
        orchestration_scope: str = "generic",
    ) -> None:
        if not isinstance(agent_runner, AgentRunner):
            raise TypeError("agent_runner must be AgentRunner")
        if not isinstance(agent, AgentSpec):
            raise TypeError("agent must be AgentSpec")
        if not isinstance(artifact_port, RunBoundArtifactPort):
            raise TypeError("artifact_port must implement RunBoundArtifactPort")
        if not isinstance(node_output_resource, HarnessNodeOutputResourcePort):
            raise TypeError(
                "node_output_resource must implement HarnessNodeOutputResourcePort"
            )
        if not isinstance(event_port, HarnessTransitionPort):
            raise TypeError("event_port must implement HarnessTransitionPort")
        if getattr(event_port, "is_durable", False) is not True:
            raise ValueError(
                "AgentLoop Graph production composition requires a durable "
                "HarnessTransitionPort; InMemoryHarnessEventPort is test-only"
            )
        if runtime_execution_composition is not None:
            if not isinstance(runtime_execution_composition, RuntimeExecutionComposition):
                raise TypeError(
                    "runtime_execution_composition must be RuntimeExecutionComposition"
                )
            runtime_execution_composition.verify_integrity()
            if agent_runner.execution_environment is not runtime_execution_composition.execution_registry:
                raise RuntimeCompositionDriftError(
                    "AgentRunner execution registry does not match runtime composition",
                    details={
                        "composition_fingerprint": runtime_execution_composition.fingerprint,
                    },
                )

        if orchestration_feature is not None and not isinstance(
            orchestration_feature,
            AgentLoopOrchestrationFeature,
        ):
            raise TypeError("orchestration_feature must be AgentLoopOrchestrationFeature")
        if orchestration_scope not in {"generic", "research_dynamic"}:
            raise ValueError(
                "orchestration_scope must be 'generic' or 'research_dynamic'"
            )
        if (
            orchestration_feature is not None
            and orchestration_feature_enabled
            and not orchestration_feature.enabled
        ):
            raise ValueError(
                "orchestration_feature_enabled conflicts with a disabled feature"
            )
        selected_feature = orchestration_feature or AgentLoopOrchestrationFeature(
            enabled=orchestration_feature_enabled,
        )
        orchestration_binding = build_agent_loop_orchestration_binding(
            feature_enabled=selected_feature.enabled_for(orchestration_scope),
            runtime=orchestration_runtime,
            # A raw callback remains a test compatibility API on the binding
            # helper, but is intentionally not a production composition path.
            dispatch=(
                orchestration_dispatch
                if not selected_feature.enabled and orchestration_runtime is None
                else None
            ),
        )
        if orchestration_binding.available and (
            orchestration_runtime is None or not orchestration_runtime.has_durable_input_admission
        ):
            raise ValueError("production AgentLoop requires durable parent input admission")
        if orchestration_binding.available:
            orchestration_runtime.require_production_child_authority()
        agent_runner.bind_orchestration(
            orchestration_port=orchestration_binding.port,
            orchestration_enabled=orchestration_binding.feature_enabled,
        )

        bundle = build_agent_loop_graph_activity_binding_bundle(
            worker_ref=worker_ref,
            activity_ref=activity_ref,
            agent_runner=agent_runner,
            agent=agent,
            artifact_recorder=AgentLoopGraphArtifactRecorder(artifact_port),
            side_effect_registry=side_effect_registry,
            input_admission=orchestration_runtime if orchestration_binding.available else None,
        )
        control_plane = HarnessControlPlane(
            event_port=event_port,
            runtime_binding_authority=bundle.authority,
            side_effect_registry=side_effect_registry,
            side_effect_store=side_effect_store,
        )
        executor = HarnessGraphPhysicalActivityExecutor(
            binding_authority=bundle.authority,
            input_resolver=control_plane,
            node_output_resource=node_output_resource,
            result_committer=None,
            supervisor=AttemptSupervisor(clock=lambda: utc_now().timestamp()),
        )
        dispatcher = HarnessGraphPhysicalActivityDispatcher(
            executor=executor,
            graph_resolver=control_plane.graph_for_activity,
            input_resolver=control_plane,
            accept=control_plane.accept_graph_activity_for_execution,
            record_call_marker=control_plane.record_graph_activity_call_marker,
            record_result=control_plane.record_graph_activity_result_event,
            apply_result=control_plane.commit_physical_graph_result,
            durable_recovery_resolver=event_port.recover_graph,
            capabilities_resolver=lambda activity: bundle.authority.resolve_activity(
                activity,
                required_usage="serial",
            ).capabilities,
        )
        control_plane.install_graph_activity_dispatcher(dispatcher)
        self._bundle = bundle
        self._control_plane = control_plane
        self._runtime_execution_composition = runtime_execution_composition
        self._orchestration_binding = orchestration_binding
        self._orchestration_feature = selected_feature

    @property
    def binding_bundle(self) -> AgentLoopGraphActivityBindingBundle:
        return self._bundle

    @property
    def control_plane(self) -> HarnessControlPlane:
        return self._control_plane

    @property
    def runtime_execution_composition(self) -> RuntimeExecutionComposition | None:
        return self._runtime_execution_composition

    @property
    def orchestration_binding(self) -> AgentOrchestrationBinding:
        """Expose the feature/availability state selected by this composition."""

        return self._orchestration_binding

    @property
    def composition_state(self) -> AgentOrchestrationCompositionState:
        """Return the immutable rollout state selected during composition."""

        return self._orchestration_binding.composition_state

    @property
    def orchestration_feature(self) -> AgentLoopOrchestrationFeature:
        """Return the immutable feature decision selected at composition time."""

        return self._orchestration_feature

    def run(self, run_spec: HarnessRunSpec) -> HarnessRunResult:
        if not isinstance(run_spec, HarnessRunSpec):
            raise TypeError("run_spec must be HarnessRunSpec")
        return self._control_plane.run(run_spec)

    def recover_and_run(self, run_spec: HarnessRunSpec) -> HarnessRunResult:
        """Resume a previously committed Graph run from durable history."""

        if not isinstance(run_spec, HarnessRunSpec):
            raise TypeError("run_spec must be HarnessRunSpec")
        return self._control_plane.recover_and_run(run_spec)

    def resume_after_approval(
        self,
        run_spec: HarnessRunSpec,
        *,
        approved: bool,
        approval_ref: str,
        actor_identity_scope_ref: str,
        reason: str | None = None,
    ) -> HarnessRunResult:
        """Resume or cancel an approval wait using durable Harness evidence."""

        if not isinstance(run_spec, HarnessRunSpec):
            raise TypeError("run_spec must be HarnessRunSpec")
        return self._control_plane.resume_after_approval(
            run_spec,
            approved=approved,
            reason=reason,
            approval_ref=approval_ref,
            actor_identity_scope_ref=actor_identity_scope_ref,
        )


def build_agent_loop_graph_application_service(
    *,
    agent_runner: AgentRunner,
    artifact_port: RunBoundArtifactPort,
    node_output_resource: HarnessNodeOutputResourcePort,
    result_committer: HarnessGraphActivityExecutionCommitPort,
    worker_ref: HarnessContractReference,
    activity_ref: HarnessContractReference,
) -> AgentLoopGraphApplicationService:
    """Compose AgentLoop's exact Graph leaf and durable owner ports.

    Storage, conversation, event, and terminal-manifest owners are supplied by
    the surrounding runtime composition. This factory intentionally creates no
    local fallback and has no terminal publication authority.
    """

    return AgentLoopGraphApplicationService(
        agent_runner=agent_runner,
        artifact_port=artifact_port,
        node_output_resource=node_output_resource,
        result_committer=result_committer,
        worker_ref=worker_ref,
        activity_ref=activity_ref,
    )


def build_agent_loop_graph_runtime_composition(
    *,
    agent_runner: AgentRunner,
    agent: AgentSpec,
    artifact_port: RunBoundArtifactPort,
    node_output_resource: HarnessNodeOutputResourcePort,
    event_port: HarnessTransitionPort,
    worker_ref: HarnessContractReference,
    activity_ref: HarnessContractReference,
    side_effect_registry: HarnessSideEffectRegistry | None = None,
    side_effect_store: HarnessSideEffectStorePort | None = None,
    runtime_execution_composition: RuntimeExecutionComposition | None = None,
    orchestration_runtime: HarnessAgentOrchestrationRuntime | None = None,
    orchestration_dispatch: AgentOrchestrationDispatch | None = None,
    orchestration_feature_enabled: bool = False,
    orchestration_feature: AgentLoopOrchestrationFeature | None = None,
    orchestration_scope: str = "generic",
) -> AgentLoopGraphRuntimeComposition:
    """Compose one durable AgentLoop Graph worker and its Harness dispatcher."""

    return AgentLoopGraphRuntimeComposition(
        agent_runner=agent_runner,
        agent=agent,
        artifact_port=artifact_port,
        node_output_resource=node_output_resource,
        event_port=event_port,
        worker_ref=worker_ref,
        activity_ref=activity_ref,
        side_effect_registry=side_effect_registry,
        side_effect_store=side_effect_store,
        runtime_execution_composition=runtime_execution_composition,
        orchestration_runtime=orchestration_runtime,
        orchestration_dispatch=orchestration_dispatch,
        orchestration_feature_enabled=orchestration_feature_enabled,
        orchestration_feature=orchestration_feature,
        orchestration_scope=orchestration_scope,
    )


def build_agent_loop_orchestration_binding(
    *,
    feature_enabled: bool,
    runtime: HarnessAgentOrchestrationRuntime | None = None,
    dispatch: AgentOrchestrationDispatch | None = None,
    composition_state: AgentOrchestrationCompositionState | str | None = None,
) -> AgentOrchestrationBinding:
    """Resolve the only generic AgentLoop-to-Harness delegation boundary.

    Production composition supplies ``runtime``, which owns strict candidate
    materialization and reuses the durable TaskPlan/group-wave runtime. The
    optional callback is retained only for isolated compatibility tests.
    """

    if runtime is not None:
        if not isinstance(runtime, HarnessAgentOrchestrationRuntime):
            raise TypeError("runtime must be HarnessAgentOrchestrationRuntime")
        state = composition_state
        if state is None:
            state = runtime.composition_state
        try:
            state = AgentOrchestrationCompositionState(state)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "composition_state must be a supported AgentLoop composition state"
            ) from exc
        if not feature_enabled or state is AgentOrchestrationCompositionState.FEATURE_DISABLED:
            return AgentOrchestrationBinding(
                feature_enabled=False,
                port=None,
                availability_reason="feature_disabled",
                composition_state=AgentOrchestrationCompositionState.FEATURE_DISABLED,
            )
        if state is AgentOrchestrationCompositionState.DEPENDENCY_UNAVAILABLE:
            raise ValueError("DEPENDENCY_UNAVAILABLE cannot bind a real orchestration runtime")
        if state is not runtime.composition_state:
            raise ValueError(
                "composition_state does not match the bound orchestration coordinator"
            )
        return AgentOrchestrationBinding(
            feature_enabled=True,
            port=runtime,
            composition_state=state,
        )
    return AgentOrchestrationBinding.from_dispatch(
        feature_enabled=feature_enabled,
        dispatch=dispatch,
        composition_state=composition_state,
    )


__all__ = [
    "AgentLoopOrchestrationFeature",
    "AgentOrchestrationCompositionState",
    "AgentLoopGraphRuntimeComposition",
    "build_agent_loop_harness_orchestration_runtime",
    "build_agent_loop_orchestration_binding",
    "build_agent_loop_planning_observation_service",
    "build_agent_loop_graph_application_service",
    "build_agent_loop_graph_runtime_composition",
]
