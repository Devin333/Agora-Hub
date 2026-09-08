from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.activity import HarnessStepSpec, HarnessWorkerType
from framework.harness.graph.definition import (
    HarnessGraphDefinition,
    HarnessGraphLeafBinding,
    HarnessGraphTaskPlanStageBinding,
)
from framework.harness.graph.dsl import (
    BoundedLoop,
    Choice,
    HarnessGraphExpression,
    ParallelAll,
    ParallelAny,
    PureMerge,
    Sequence,
    StepRef,
    VerifiedAggregation,
    Wait,
    WaitTimeoutAction,
)
from framework.harness.graph.model import (
    HarnessBranch,
    HarnessCommittedOutputReference,
    HarnessCompensationReference,
    HarnessContractKind,
    HarnessContractReference,
    HarnessControlNode,
    HarnessExecutableNode,
    HarnessGraphEdge,
    HarnessGraphEdgeKind,
    HarnessGraphNode,
    HarnessGraphNodeKind,
    HarnessJoinContract,
    HarnessLoopContract,
    HarnessMergeContract,
    HarnessMergeKind,
    HarnessRepairReference,
    HarnessWaitContract,
    NormalizedHarnessGraph,
)
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)


_TASK_PLAN_AUTHORITY_METADATA_KEYS = frozenset(
    {
        "dynamic_stage",
        "required_output_roles",
        "task_plan_policy_ref",
        "task_plan_schema",
        "task_plan_support",
        "agent_loop_delegation",
    }
)


@dataclass(frozen=True, slots=True)
class HarnessGraphCompileResult:
    """Graph-only compilation result bound to one exact definition."""

    graph: NormalizedHarnessGraph
    definition_checksum: str
    compiler_version: str = HARNESS_GRAPH_ONLY_COMPILER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.graph, NormalizedHarnessGraph):
            raise TypeError("graph must be NormalizedHarnessGraph")
        if self.compiler_version != HARNESS_GRAPH_ONLY_COMPILER_VERSION:
            raise HarnessValidationError(
                "unsupported Graph-only compiler version",
                code="unsupported_graph_compiler",
                details={"compiler_version": str(self.compiler_version)},
            )
        if self.graph.compiler_version != self.compiler_version:
            raise HarnessValidationError(
                "compile result does not match the pinned Graph compiler",
                code="graph_compiler_version_mismatch",
                details={
                    "result_compiler_version": self.compiler_version,
                    "graph_compiler_version": self.graph.compiler_version,
                },
            )
        if self.graph.definition_checksum != self.definition_checksum:
            raise HarnessValidationError(
                "compile result does not match its Graph definition",
                code="graph_definition_lineage_mismatch",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "compiler_version": self.compiler_version,
            "definition_checksum": self.definition_checksum,
            "normalized_graph_checksum": self.graph.checksum,
            "graph": self.graph.to_dict(),
        }


class HarnessGraphCompiler:
    """Pure compiler for explicit Graph definitions.

    This compiler has no retired orchestration declaration, legacy reader, registry default,
    or runtime admission fallback. Its output is the only production admission
    input accepted by the Harness control plane.
    """

    compiler_version = HARNESS_GRAPH_ONLY_COMPILER_VERSION

    def compile(self, definition: HarnessGraphDefinition) -> HarnessGraphCompileResult:
        if not isinstance(definition, HarnessGraphDefinition):
            raise TypeError("definition must be HarnessGraphDefinition")
        definition.verify_integrity()
        context = _CompilerContext(definition)
        graph = context.compile()
        assert definition.definition_checksum is not None
        return HarnessGraphCompileResult(
            graph=graph,
            definition_checksum=definition.definition_checksum,
        )


@dataclass(frozen=True, slots=True)
class _Fragment:
    entry_node_ids: tuple[str, ...]
    terminal_node_ids: tuple[str, ...]


class _CompilerContext:
    def __init__(self, definition: HarnessGraphDefinition) -> None:
        self.definition = definition
        self.activities_by_id = {
            activity.step_id: activity for activity in definition.activities
        }
        self.nodes: list[HarnessGraphNode] = []
        self.edges: list[HarnessGraphEdge] = []
        self.compensation_refs: list[HarnessCompensationReference] = []
        self.repair_refs: list[HarnessRepairReference] = []
        self._node_order = 0
        self._edge_order = 0

    def compile(self) -> NormalizedHarnessGraph:
        fragment = self._compile_expression(self.definition.root.root)
        self._compile_compensations()
        self._compile_repairs()
        return self._build_graph(fragment)

    def _compile_expression(self, expression: HarnessGraphExpression) -> _Fragment:
        if isinstance(expression, StepRef):
            node_id = expression.node_id or expression.step_id
            self._add_executable(expression.step_id, node_id)
            return _Fragment((node_id,), (node_id,))
        if isinstance(expression, Sequence):
            fragments = tuple(
                self._compile_expression(child) for child in expression.children
            )
            for left, right in zip(fragments, fragments[1:]):
                for source_id in left.terminal_node_ids:
                    for target_id in right.entry_node_ids:
                        self._add_edge(
                            source_id,
                            target_id,
                            self._sequence_edge_kind(source_id),
                        )
            return _Fragment(
                fragments[0].entry_node_ids,
                fragments[-1].terminal_node_ids,
            )
        if isinstance(expression, Choice):
            return self._compile_choice(expression)
        if isinstance(expression, ParallelAll):
            return self._compile_parallel_all(expression)
        if isinstance(expression, ParallelAny):
            return self._compile_parallel_any(expression)
        if isinstance(expression, BoundedLoop):
            return self._compile_loop(expression)
        if isinstance(expression, Wait):
            self._add_control(
                node_id=expression.wait_id,
                node_kind=HarnessGraphNodeKind.WAIT,
                wait=HarnessWaitContract(
                    wait_id=expression.wait_id,
                    kind=expression.kind,
                    correlation=expression.correlation,
                    signal_type=expression.signal_type,
                    signal_version=expression.signal_version,
                    tenant_scope_path=expression.tenant_scope_path,
                    identity_scope_path=expression.identity_scope_path,
                    timeout_policy=expression.timeout_policy,
                    deadline_input_path=expression.deadline_input_path,
                ),
            )
            if (
                expression.timeout_policy is not None
                and expression.timeout_policy.action is WaitTimeoutAction.ROUTE
            ):
                assert expression.timeout_policy.target_node_id is not None
                self._add_edge(
                    expression.wait_id,
                    expression.timeout_policy.target_node_id,
                    HarnessGraphEdgeKind.WAIT_TIMEOUT,
                )
            return _Fragment((expression.wait_id,), (expression.wait_id,))
        raise HarnessValidationError(
            "compiler received unsupported Graph expression",
            code="unsupported_graph_node_kind",
            details={"type": type(expression).__name__},
        )

    def _sequence_edge_kind(self, source_id: str) -> HarnessGraphEdgeKind:
        source = next(
            (node for node in self.nodes if node.node_id == source_id),
            None,
        )
        if (
            isinstance(source, HarnessControlNode)
            and source.node_kind is HarnessGraphNodeKind.WAIT
        ):
            return HarnessGraphEdgeKind.WAIT_RESUME
        return HarnessGraphEdgeKind.DEPENDENCY

    def _compile_choice(self, expression: Choice) -> _Fragment:
        compiled: list[tuple[Any, _Fragment]] = []
        branches: list[HarnessBranch] = []
        for branch in expression.branches:
            fragment = self._compile_expression(branch.child)
            compiled.append((branch, fragment))
            branches.append(
                HarnessBranch(
                    branch_id=branch.branch_id,
                    entry_node_ids=fragment.entry_node_ids,
                    terminal_node_ids=fragment.terminal_node_ids,
                    priority=branch.priority,
                    output_namespace=(
                        branch.output_namespace
                        or f"choice.{expression.choice_id}.{branch.branch_id}"
                    ),
                    condition=branch.condition,
                    is_default=branch.is_default,
                )
            )
        self._add_control(
            node_id=expression.choice_id,
            node_kind=HarnessGraphNodeKind.CHOICE,
            branches=tuple(branches),
        )
        choice_join_id = f"{expression.choice_id}:join"
        self._add_control(
            node_id=choice_join_id,
            node_kind=HarnessGraphNodeKind.CHOICE_JOIN,
            branches=tuple(branches),
            metadata={"choice_node_id": expression.choice_id},
        )
        for branch, fragment in compiled:
            edge_kind = (
                HarnessGraphEdgeKind.DEFAULT
                if branch.is_default
                else HarnessGraphEdgeKind.CHOICE
            )
            for target_id in fragment.entry_node_ids:
                self._add_edge(
                    expression.choice_id,
                    target_id,
                    edge_kind,
                    priority=branch.priority,
                    condition=branch.condition,
                    branch_id=branch.branch_id,
                )
            for terminal_id in fragment.terminal_node_ids:
                self._add_edge(
                    terminal_id,
                    choice_join_id,
                    HarnessGraphEdgeKind.DEPENDENCY,
                    branch_id=branch.branch_id,
                )
        return _Fragment((expression.choice_id,), (choice_join_id,))

    def _compile_parallel_all(self, expression: ParallelAll) -> _Fragment:
        compiled = tuple(
            (branch, self._compile_expression(branch.child))
            for branch in expression.branches
        )
        branches = tuple(
            HarnessBranch(
                branch_id=branch.branch_id,
                entry_node_ids=fragment.entry_node_ids,
                terminal_node_ids=fragment.terminal_node_ids,
                priority=index,
                output_namespace=branch.output_namespace,
                is_default=False,
            )
            for index, (branch, fragment) in enumerate(compiled)
        )
        merge_node_id = f"{expression.join_id}:merge"
        merge_ref = None
        if isinstance(expression.merge, PureMerge):
            merge_ref = _exact_reference(
                HarnessContractKind.MERGE,
                expression.merge.merge_ref,
                field_name="parallel_all.merge.merge_ref",
            )
        self._add_control(
            node_id=expression.fork_id,
            node_kind=HarnessGraphNodeKind.FORK_ALL,
            branches=branches,
        )
        self._add_control(
            node_id=expression.join_id,
            node_kind=HarnessGraphNodeKind.JOIN_ALL,
            join=HarnessJoinContract(
                fork_node_id=expression.fork_id,
                required_branch_ids=tuple(
                    branch.branch_id for branch in expression.branches
                ),
                failure_policy=expression.failure_policy.value,
                merge_ref=merge_ref,
            ),
        )
        for branch, fragment in compiled:
            for entry_id in fragment.entry_node_ids:
                self._add_edge(
                    expression.fork_id,
                    entry_id,
                    HarnessGraphEdgeKind.FORK_BRANCH,
                    branch_id=branch.branch_id,
                )
            for terminal_id in fragment.terminal_node_ids:
                self._add_edge(
                    terminal_id,
                    expression.join_id,
                    HarnessGraphEdgeKind.JOIN,
                    branch_id=branch.branch_id,
                )
        if expression.merge is None:
            return _Fragment((expression.fork_id,), (expression.join_id,))
        if isinstance(expression.merge, PureMerge):
            self._add_control(
                node_id=merge_node_id,
                node_kind=HarnessGraphNodeKind.MERGE,
                merge=HarnessMergeContract(
                    merge_kind=HarnessMergeKind.PURE,
                    input_branch_ids=tuple(
                        branch.branch_id for branch in expression.branches
                    ),
                    output_keys=expression.merge.output_keys,
                    merge_ref=merge_ref,
                ),
                metadata={"join_node_id": expression.join_id},
            )
            self._add_edge(
                expression.join_id,
                merge_node_id,
                HarnessGraphEdgeKind.DEPENDENCY,
            )
            return _Fragment((expression.fork_id,), (merge_node_id,))
        if not isinstance(expression.merge, VerifiedAggregation):
            raise AssertionError("unsupported Parallel-All merge contract")
        aggregation_activity = self._activity(expression.merge.step.step_id)
        if expression.merge.branch_inputs_key not in aggregation_activity.input_keys:
            raise HarnessValidationError(
                "verified aggregation activity must declare its branch input key",
                code="aggregation_input_key_missing",
                details={
                    "activity_id": aggregation_activity.step_id,
                    "branch_inputs_key": expression.merge.branch_inputs_key,
                },
            )
        if aggregation_activity.output_key is None:
            raise HarnessValidationError(
                "verified aggregation activity must declare one output key",
                code="aggregation_output_key_missing",
                details={"activity_id": aggregation_activity.step_id},
            )
        if aggregation_activity.quality_gate is None:
            raise HarnessValidationError(
                "verified aggregation activity requires an exact deterministic Gate",
                code="aggregation_gate_missing",
                details={"activity_id": aggregation_activity.step_id},
            )
        aggregation = self._compile_expression(expression.merge.step)
        if (
            len(aggregation.entry_node_ids) != 1
            or len(aggregation.terminal_node_ids) != 1
        ):
            raise HarnessValidationError(
                "verified aggregation must compile to one executable node",
                code="invalid_merge_contract",
            )
        aggregation_node_id = aggregation.entry_node_ids[0]
        self._add_control(
            node_id=merge_node_id,
            node_kind=HarnessGraphNodeKind.MERGE,
            merge=HarnessMergeContract(
                merge_kind=HarnessMergeKind.AGGREGATION_STEP,
                input_branch_ids=tuple(
                    branch.branch_id for branch in expression.branches
                ),
                output_keys=(aggregation_activity.output_key,),
                aggregation_node_id=aggregation_node_id,
            ),
            metadata={
                "branch_inputs_key": expression.merge.branch_inputs_key,
                "join_node_id": expression.join_id,
            },
        )
        self._add_edge(
            expression.join_id,
            aggregation_node_id,
            HarnessGraphEdgeKind.DEPENDENCY,
        )
        for terminal_id in aggregation.terminal_node_ids:
            self._add_edge(
                terminal_id,
                merge_node_id,
                HarnessGraphEdgeKind.DEPENDENCY,
            )
        return _Fragment((expression.fork_id,), (merge_node_id,))

    def _compile_parallel_any(self, expression: ParallelAny) -> _Fragment:
        compiled = tuple(
            (branch, self._compile_expression(branch.child))
            for branch in expression.branches
        )
        branches = tuple(
            HarnessBranch(
                branch_id=branch.branch_id,
                entry_node_ids=fragment.entry_node_ids,
                terminal_node_ids=fragment.terminal_node_ids,
                priority=index,
                output_namespace=branch.output_namespace,
                is_default=False,
            )
            for index, (branch, fragment) in enumerate(compiled)
        )
        self._add_control(
            node_id=expression.fork_id,
            node_kind=HarnessGraphNodeKind.FORK_ANY,
            branches=branches,
            metadata={"cancellation_policy": expression.cancellation_policy.value},
        )
        self._add_control(
            node_id=expression.join_id,
            node_kind=HarnessGraphNodeKind.JOIN_ANY,
            join=HarnessJoinContract(
                fork_node_id=expression.fork_id,
                required_branch_ids=tuple(
                    branch.branch_id for branch in expression.branches
                ),
                failure_policy=expression.failure_policy.value,
                winner_policy="first_verified_success_by_stream_sequence",
            ),
        )
        for branch, fragment in compiled:
            for entry_id in fragment.entry_node_ids:
                self._add_edge(
                    expression.fork_id,
                    entry_id,
                    HarnessGraphEdgeKind.FORK_BRANCH,
                    branch_id=branch.branch_id,
                )
            for terminal_id in fragment.terminal_node_ids:
                self._add_edge(
                    terminal_id,
                    expression.join_id,
                    HarnessGraphEdgeKind.JOIN,
                    branch_id=branch.branch_id,
                )
        return _Fragment((expression.fork_id,), (expression.join_id,))

    def _compile_loop(self, expression: BoundedLoop) -> _Fragment:
        if expression.exit is None:
            raise HarnessValidationError(
                "bounded loop requires an explicit exit expression",
                code="loop_exit_missing",
                details={"loop_id": expression.loop_id},
            )
        body = self._compile_expression(expression.body)
        exit_fragment = self._compile_expression(expression.exit)
        exhaustion = (
            None
            if expression.exhaustion is None
            else self._compile_expression(expression.exhaustion)
        )
        self._add_control(
            node_id=expression.loop_id,
            node_kind=HarnessGraphNodeKind.LOOP_GUARD,
            loop=HarnessLoopContract(
                body_entry_node_ids=body.entry_node_ids,
                body_terminal_node_ids=body.terminal_node_ids,
                condition=expression.condition,
                max_iterations=expression.max_iterations,
                exit_node_ids=exit_fragment.entry_node_ids,
                exhaustion_node_ids=(
                    () if exhaustion is None else exhaustion.entry_node_ids
                ),
            ),
        )
        loop_join_id = f"{expression.loop_id}:join"
        loop_routes = [
            HarnessBranch(
                branch_id="exit",
                entry_node_ids=exit_fragment.entry_node_ids,
                terminal_node_ids=exit_fragment.terminal_node_ids,
                priority=0,
                output_namespace=f"loop.{expression.loop_id}.exit",
            )
        ]
        if exhaustion is not None:
            loop_routes.append(
                HarnessBranch(
                    branch_id="exhaustion",
                    entry_node_ids=exhaustion.entry_node_ids,
                    terminal_node_ids=exhaustion.terminal_node_ids,
                    priority=1,
                    output_namespace=f"loop.{expression.loop_id}.exhaustion",
                )
            )
        self._add_control(
            node_id=loop_join_id,
            node_kind=HarnessGraphNodeKind.LOOP_JOIN,
            branches=tuple(loop_routes),
            metadata={"loop_node_id": expression.loop_id},
        )
        for entry_id in body.entry_node_ids:
            self._add_edge(
                expression.loop_id,
                entry_id,
                HarnessGraphEdgeKind.LOOP_BODY,
                condition=expression.condition,
                loop_id=expression.loop_id,
            )
        for terminal_id in body.terminal_node_ids:
            self._add_edge(
                terminal_id,
                expression.loop_id,
                HarnessGraphEdgeKind.LOOP_BACK,
                loop_id=expression.loop_id,
            )
        for entry_id in exit_fragment.entry_node_ids:
            self._add_edge(
                expression.loop_id,
                entry_id,
                HarnessGraphEdgeKind.LOOP_EXIT,
                loop_id=expression.loop_id,
            )
        if exhaustion is not None:
            for entry_id in exhaustion.entry_node_ids:
                self._add_edge(
                    expression.loop_id,
                    entry_id,
                    HarnessGraphEdgeKind.LOOP_EXHAUSTED,
                    loop_id=expression.loop_id,
                )
        for route in loop_routes:
            for terminal_id in route.terminal_node_ids:
                self._add_edge(
                    terminal_id,
                    loop_join_id,
                    HarnessGraphEdgeKind.DEPENDENCY,
                    branch_id=route.branch_id,
                    loop_id=expression.loop_id,
                )
        return _Fragment((expression.loop_id,), (loop_join_id,))

    def _compile_compensations(self) -> None:
        for binding in sorted(
            self.definition.root.compensations,
            key=lambda item: item.binding_id,
        ):
            compensation_node_id = f"compensation:{binding.binding_id}"
            self._add_executable(
                binding.compensation_step_id,
                compensation_node_id,
            )
            node = self._executable_node(compensation_node_id)
            declared_activity_ref = _exact_reference(
                HarnessContractKind.ACTIVITY,
                binding.activity_contract_ref,
                field_name="compensation.activity_contract_ref",
            )
            if node.activity_ref != declared_activity_ref:
                raise HarnessValidationError(
                    "compensation binding does not match selected activity contract",
                    code="graph_compensation_activity_reference_mismatch",
                    details={
                        "binding_id": binding.binding_id,
                        "declared": declared_activity_ref.exact_ref,
                        "selected": node.activity_ref.exact_ref,
                    },
                )
            self.compensation_refs.append(
                HarnessCompensationReference(
                    binding_id=binding.binding_id,
                    for_node_id=binding.for_node_id,
                    compensation_node_id=compensation_node_id,
                    handler_ref=_exact_reference(
                        HarnessContractKind.COMPENSATION,
                        binding.handler_ref,
                        field_name="compensation.handler_ref",
                    ),
                    activity_ref=node.activity_ref,
                    scope=binding.scope,
                )
            )
            self._add_edge(
                binding.for_node_id,
                compensation_node_id,
                HarnessGraphEdgeKind.COMPENSATION,
            )

    def _compile_repairs(self) -> None:
        for binding in self.definition.repair_bindings:
            self._add_executable(
                binding.repair_activity_id,
                binding.repair_node_id,
            )
            source = self._executable_node(binding.source_node_id)
            repair = self._executable_node(binding.repair_node_id)
            self.repair_refs.append(
                HarnessRepairReference(
                    binding_id=binding.binding_id,
                    source_activity_id=source.step_id,
                    source_node_id=binding.source_node_id,
                    repair_activity_id=binding.repair_activity_id,
                    repair_activity_ref=repair.activity_ref,
                    repair_node_id=binding.repair_node_id,
                    triggers=tuple(trigger.value for trigger in binding.triggers),
                )
            )
            self._add_edge(
                binding.source_node_id,
                binding.repair_node_id,
                HarnessGraphEdgeKind.REPAIR,
            )

    def _add_executable(self, activity_id: str, node_id: str) -> None:
        activity = self._activity(activity_id)
        leaf_binding = self.definition.leaf_activity_binding(activity_id)
        task_plan_binding = self.definition.task_plan_stage_binding(activity_id)
        delegated = (
            activity.worker_type is HarnessWorkerType.AGENT_LOOP
            and leaf_binding is not None and task_plan_binding is not None
        )
        if (leaf_binding is None) == (task_plan_binding is None) and not delegated:
            raise HarnessValidationError(
                "Graph activity must resolve to exactly one binding class",
                code="graph_activity_binding_resolution_invalid",
                details={"activity_id": activity_id},
            )
        worker_ref, activity_ref, step_metadata = self._binding_projection(
            activity,
            leaf_binding=leaf_binding,
            task_plan_binding=task_plan_binding,
        )
        gate_refs = ()
        if activity.quality_gate is not None:
            gate_refs = (
                _exact_reference(
                    HarnessContractKind.GATE,
                    activity.quality_gate,
                    field_name=f"activity[{activity.step_id}].quality_gate",
                ),
            )
        side_effect_ref = None
        if activity.side_effect_ref is not None:
            side_effect_ref = HarnessContractReference(
                HarnessContractKind.SIDE_EFFECT,
                activity.side_effect_ref.handler_id,
                activity.side_effect_ref.version,
            )
        self.nodes.append(
            HarnessExecutableNode(
                node_id=node_id,
                step_id=activity.step_id,
                declaration_order=self._next_node_order(),
                step_ref=HarnessContractReference(
                    HarnessContractKind.STEP,
                    f"{self.definition.graph_id}:{activity.step_id}",
                    self.definition.graph_version,
                ),
                worker_ref=worker_ref,
                activity_ref=activity_ref,
                gate_refs=gate_refs,
                side_effect_ref=side_effect_ref,
                input_keys=activity.input_keys,
                output_keys=(
                    () if activity.output_key is None else (activity.output_key,)
                ),
                metadata={
                    "binding_source": "graph_definition",
                    "worker_type": activity.worker_type.value,
                    "step_metadata": step_metadata,
                    "retry_policy": activity.retry_policy.to_dict(),
                },
            )
        )

    def _binding_projection(
        self,
        activity: HarnessStepSpec,
        *,
        leaf_binding: HarnessGraphLeafBinding | None,
        task_plan_binding: HarnessGraphTaskPlanStageBinding | None,
    ) -> tuple[HarnessContractReference, HarnessContractReference, dict[str, Any]]:
        metadata = dict(activity.metadata)
        if leaf_binding is not None:
            if activity.worker_type is not leaf_binding.expected_worker_type:
                raise HarnessValidationError(
                    "Graph leaf binding does not match activity worker type",
                    code="graph_leaf_activity_worker_type_mismatch",
                    details={"activity_id": activity.step_id},
                )
            if task_plan_binding is None:
                if activity.worker_type is HarnessWorkerType.AGENT_LOOP and _TASK_PLAN_AUTHORITY_METADATA_KEYS.intersection(metadata):
                    raise HarnessValidationError(
                        "AgentLoop delegation authority requires an explicit TaskPlan binding",
                        code="graph_task_plan_authority_metadata_forbidden",
                    )
                return leaf_binding.worker_ref, leaf_binding.activity_ref, metadata
            if activity.worker_type is not HarnessWorkerType.AGENT_LOOP or (
                leaf_binding.worker_ref, leaf_binding.activity_ref
            ) != (task_plan_binding.worker_ref, task_plan_binding.activity_ref):
                raise HarnessValidationError(
                    "AgentLoop delegation differs from its leaf binding",
                    code="graph_agent_delegation_binding_mismatch",
                )
        assert task_plan_binding is not None
        if activity.worker_type not in {HarnessWorkerType.TASK_PLAN, HarnessWorkerType.AGENT_LOOP}:
            raise HarnessValidationError(
                "Graph TaskPlan binding does not target a TaskPlan activity",
                code="graph_task_plan_stage_binding_worker_type_mismatch",
                details={"activity_id": activity.step_id},
            )
        forbidden = sorted(_TASK_PLAN_AUTHORITY_METADATA_KEYS.intersection(metadata))
        if forbidden:
            raise HarnessValidationError(
                "Graph TaskPlan authority must come from its exact stage binding",
                code="graph_task_plan_authority_metadata_forbidden",
                details={"activity_id": activity.step_id, "fields": forbidden},
            )
        metadata.update(
            {
                "dynamic_stage": True,
                "task_plan_policy_ref": task_plan_binding.policy_ref,
                "task_plan_schema": task_plan_binding.task_plan_schema,
                "required_output_roles": list(
                    task_plan_binding.required_output_roles
                ),
                "task_plan_support": dict(task_plan_binding.support_refs),
            }
        )
        if activity.worker_type is HarnessWorkerType.AGENT_LOOP:
            metadata["agent_loop_delegation"] = True
        return (
            task_plan_binding.worker_ref,
            task_plan_binding.activity_ref,
            metadata,
        )

    def _add_control(
        self,
        *,
        node_id: str,
        node_kind: HarnessGraphNodeKind,
        branches: tuple[HarnessBranch, ...] = (),
        join: HarnessJoinContract | None = None,
        loop: HarnessLoopContract | None = None,
        wait: HarnessWaitContract | None = None,
        merge: HarnessMergeContract | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.nodes.append(
            HarnessControlNode(
                node_id=node_id,
                node_kind=node_kind,
                declaration_order=self._next_node_order(),
                branches=branches,
                join=join,
                loop=loop,
                wait=wait,
                merge=merge,
                metadata=metadata or {},
            )
        )

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_kind: HarnessGraphEdgeKind,
        *,
        priority: int = 0,
        condition: Any = None,
        branch_id: str | None = None,
        loop_id: str | None = None,
    ) -> None:
        ordinal = self._edge_order
        self._edge_order += 1
        self.edges.append(
            HarnessGraphEdge(
                edge_id=(
                    f"edge:{ordinal:06d}:{edge_kind.value}:{source_id}:{target_id}"
                ),
                source_id=source_id,
                target_id=target_id,
                edge_kind=edge_kind,
                priority=priority,
                condition=condition,
                branch_id=branch_id,
                loop_id=loop_id,
            )
        )

    def _build_graph(self, fragment: _Fragment) -> NormalizedHarnessGraph:
        policy = self.definition.terminal_side_effect_policy
        failure_policy = self.definition.terminal_failure_side_effect_policy
        return NormalizedHarnessGraph(
            graph_id=self.definition.graph_id,
            graph_version=self.definition.graph_version,
            graph_ref=HarnessContractReference(
                HarnessContractKind.GRAPH,
                self.definition.graph_id,
                self.definition.graph_version,
            ),
            definition_schema_version=self.definition.schema_version,
            definition_checksum=self.definition.definition_checksum,
            nodes=tuple(self.nodes),
            edges=tuple(self.edges),
            entry_node_ids=fragment.entry_node_ids,
            terminal_node_ids=fragment.terminal_node_ids,
            input_keys=self.definition.root.input_keys,
            terminal_output_keys=self.definition.root.terminal_output_keys,
            compensation_refs=tuple(self.compensation_refs),
            committed_output_refs=self._committed_output_refs(),
            repair_refs=tuple(self.repair_refs),
            terminal_policy_ref=HarnessContractReference(
                HarnessContractKind.TERMINAL_POLICY,
                policy.policy_id,
                policy.version,
            ),
            terminal_policy=policy,
            terminal_failure_policy_ref=(
                None
                if failure_policy is None
                else HarnessContractReference(
                    HarnessContractKind.TERMINAL_POLICY,
                    failure_policy.policy_id,
                    failure_policy.version,
                )
            ),
            terminal_failure_policy=failure_policy,
            schema_version=GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
            compiler_version=HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        )

    def _committed_output_refs(
        self,
    ) -> tuple[HarnessCommittedOutputReference, ...]:
        result: list[HarnessCommittedOutputReference] = []
        for binding in self.definition.committed_output_bindings:
            producer = self._executable_node(binding.producer_node_id)
            consumer = self._executable_node(binding.consumer_node_id)
            result.append(
                HarnessCommittedOutputReference(
                    binding_id=binding.binding_id,
                    producer_activity_id=binding.producer_activity_id,
                    producer_activity_ref=producer.activity_ref,
                    producer_node_id=binding.producer_node_id,
                    producer_output_key=binding.producer_output_key,
                    consumer_activity_id=binding.consumer_activity_id,
                    consumer_activity_ref=consumer.activity_ref,
                    consumer_node_id=binding.consumer_node_id,
                    receipt_input_key=binding.receipt_input_key,
                )
            )
        return tuple(result)

    def _activity(self, activity_id: str) -> HarnessStepSpec:
        try:
            return self.activities_by_id[activity_id]
        except KeyError as exc:  # pragma: no cover - GraphDefinition invariant
            raise HarnessValidationError(
                "Graph activity reference does not resolve",
                code="unknown_graph_activity_reference",
                details={"activity_id": str(activity_id)},
            ) from exc

    def _executable_node(self, node_id: str) -> HarnessExecutableNode:
        for node in self.nodes:
            if isinstance(node, HarnessExecutableNode) and node.node_id == node_id:
                return node
        raise HarnessValidationError(
            "Graph binding does not resolve to an executable node",
            code="unknown_graph_executable_node",
            details={"node_id": str(node_id)},
        )

    def _next_node_order(self) -> int:
        value = self._node_order
        self._node_order += 1
        return value


def _exact_reference(
    kind: HarnessContractKind,
    value: str,
    *,
    field_name: str,
) -> HarnessContractReference:
    text = str(value).strip()
    if text.count("@") != 1:
        raise HarnessValidationError(
            f"{field_name} must be an exact version reference",
            code="graph_inexact_version_reference",
            details={"field": field_name, "reference": text},
        )
    contract_id, version = text.rsplit("@", maxsplit=1)
    return HarnessContractReference(kind, contract_id, version)


__all__ = ["HarnessGraphCompileResult", "HarnessGraphCompiler"]
