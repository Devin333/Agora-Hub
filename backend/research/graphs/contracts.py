from __future__ import annotations

from types import MappingProxyType

from framework.events.canonical import checksum_for
from framework.harness.graph import HarnessTerminalSideEffectPolicy

from backend.research.ports.artifact_publication import (
    RESEARCH_ARTIFACT_EFFECT_KIND,
    RESEARCH_ARTIFACT_HANDLER_REF,
)


RESEARCH_PAPER_ANALYSIS_GRAPH_ID = "research.paper_analysis.graph"
RESEARCH_DYNAMIC_PAPER_ANALYSIS_GRAPH_ID = (
    "research.paper_analysis.dynamic.graph"
)
RESEARCH_PAPER_ANALYSIS_GRAPH_VERSION = "1"

RESEARCH_ARTIFACT_TERMINAL_POLICY_ID = "research.artifact.publication"
RESEARCH_ARTIFACT_TERMINAL_POLICY_VERSION = "1"
RESEARCH_ARTIFACT_NOT_REQUIRED_EVIDENCE_REF = checksum_for(
    {
        "policy": "not_required",
        "handler": RESEARCH_ARTIFACT_HANDLER_REF,
        "policy_id": RESEARCH_ARTIFACT_TERMINAL_POLICY_ID,
        "version": RESEARCH_ARTIFACT_TERMINAL_POLICY_VERSION,
    }
)

RESEARCH_DYNAMIC_STAGE_ID = "dynamic_analysis_stage"
RESEARCH_DYNAMIC_POLICY_REF = "research.analysis@1"
RESEARCH_DYNAMIC_CAPABILITIES = (
    "research.analysis.structure",
    "research.analysis.contribution",
    "research.analysis.experiments",
)
RESEARCH_DYNAMIC_OUTPUT_ROLES = (
    "analysis.structure",
    "analysis.contribution",
    "analysis.experiments",
)
RESEARCH_DYNAMIC_INPUT_REFS = ("document", "evidence_pack")
RESEARCH_DYNAMIC_GATE_REFS = (
    "SummarySchemaGate@1",
    "SummaryEvidenceCoverageGate@1",
    "BenchmarkEvidenceLineageGate@1",
)
RESEARCH_DYNAMIC_AGGREGATOR_REF = "research.analysis-aggregator@1"
RESEARCH_DYNAMIC_CANDIDATE_BUILDER_REF = "research.task-plan-builder@1"
RESEARCH_DYNAMIC_CAPABILITY_REGISTRY_REF = (
    "research.task-plan-capabilities@1"
)
RESEARCH_DYNAMIC_GATE_REGISTRY_REF = "research.paper-analysis-gates@1"
RESEARCH_DYNAMIC_RESULT_STORE_REF = "research.task-plan-results@1"
RESEARCH_DYNAMIC_TOOL_IDS = ("retrieval.read_source",)
RESEARCH_DYNAMIC_MEMORY_NAMESPACES = ("research.analysis",)

RESEARCH_DYNAMIC_SUBAGENT_IDS = MappingProxyType(
    {
        "research.analysis.structure": "research_analysis_structure",
        "research.analysis.contribution": "research_analysis_contribution",
        "research.analysis.experiments": "research_analysis_experiments",
    }
)
RESEARCH_DYNAMIC_WORKER_REFS = MappingProxyType(
    {
        capability: f"{capability}@1"
        for capability in RESEARCH_DYNAMIC_CAPABILITIES
    }
)
RESEARCH_DYNAMIC_WORKER_CONTRACT_REFS = MappingProxyType(
    {
        capability: f"{capability}.worker-contract@1"
        for capability in RESEARCH_DYNAMIC_CAPABILITIES
    }
)
RESEARCH_DYNAMIC_OUTPUT_SCHEMA_REFS = MappingProxyType(
    {
        "research.analysis.structure": "research.analysis.structure@1",
        "research.analysis.contribution": "research.analysis.contribution@1",
        "research.analysis.experiments": "research.analysis.experiments@1",
    }
)
RESEARCH_DYNAMIC_OUTPUT_ROLES_BY_CAPABILITY = MappingProxyType(
    {
        "research.analysis.structure": "analysis.structure",
        "research.analysis.contribution": "analysis.contribution",
        "research.analysis.experiments": "analysis.experiments",
    }
)
RESEARCH_DYNAMIC_GATES_BY_CAPABILITY = MappingProxyType(
    {
        "research.analysis.structure": ("SummarySchemaGate@1",),
        "research.analysis.contribution": (
            "SummaryEvidenceCoverageGate@1",
        ),
        "research.analysis.experiments": (
            "BenchmarkEvidenceLineageGate@1",
        ),
    }
)
RESEARCH_DYNAMIC_TASK_PLAN_SUPPORT_REFS = MappingProxyType(
    {
        "candidate_builder_ref": RESEARCH_DYNAMIC_CANDIDATE_BUILDER_REF,
        "capability_registry_ref": RESEARCH_DYNAMIC_CAPABILITY_REGISTRY_REF,
        "gate_registry_ref": RESEARCH_DYNAMIC_GATE_REGISTRY_REF,
        "aggregator_ref": RESEARCH_DYNAMIC_AGGREGATOR_REF,
        "checkpoint_ref": "harness.graph-checkpoint@1",
        "result_store_ref": RESEARCH_DYNAMIC_RESULT_STORE_REF,
        "event_schema": "newsroom.harness-task-plan-event/v3",
    }
)


def build_research_artifact_terminal_policy() -> (
    HarnessTerminalSideEffectPolicy
):
    return HarnessTerminalSideEffectPolicy(
        policy_id=RESEARCH_ARTIFACT_TERMINAL_POLICY_ID,
        version=RESEARCH_ARTIFACT_TERMINAL_POLICY_VERSION,
        handler=RESEARCH_ARTIFACT_HANDLER_REF,
        kind=RESEARCH_ARTIFACT_EFFECT_KIND,
        requires_approval=False,
        retry_limit=2,
        not_required_evidence_ref=(
            RESEARCH_ARTIFACT_NOT_REQUIRED_EVIDENCE_REF
        ),
        inherited_gate_refs=("ResearchQualityGate@1",),
    )


__all__ = [
    "RESEARCH_ARTIFACT_NOT_REQUIRED_EVIDENCE_REF",
    "RESEARCH_ARTIFACT_TERMINAL_POLICY_ID",
    "RESEARCH_ARTIFACT_TERMINAL_POLICY_VERSION",
    "RESEARCH_DYNAMIC_AGGREGATOR_REF",
    "RESEARCH_DYNAMIC_CAPABILITIES",
    "RESEARCH_DYNAMIC_CAPABILITY_REGISTRY_REF",
    "RESEARCH_DYNAMIC_CANDIDATE_BUILDER_REF",
    "RESEARCH_DYNAMIC_GATE_REFS",
    "RESEARCH_DYNAMIC_GATES_BY_CAPABILITY",
    "RESEARCH_DYNAMIC_GATE_REGISTRY_REF",
    "RESEARCH_DYNAMIC_INPUT_REFS",
    "RESEARCH_DYNAMIC_MEMORY_NAMESPACES",
    "RESEARCH_DYNAMIC_OUTPUT_ROLES",
    "RESEARCH_DYNAMIC_OUTPUT_ROLES_BY_CAPABILITY",
    "RESEARCH_DYNAMIC_OUTPUT_SCHEMA_REFS",
    "RESEARCH_DYNAMIC_PAPER_ANALYSIS_GRAPH_ID",
    "RESEARCH_DYNAMIC_POLICY_REF",
    "RESEARCH_DYNAMIC_RESULT_STORE_REF",
    "RESEARCH_DYNAMIC_STAGE_ID",
    "RESEARCH_DYNAMIC_SUBAGENT_IDS",
    "RESEARCH_DYNAMIC_TASK_PLAN_SUPPORT_REFS",
    "RESEARCH_DYNAMIC_TOOL_IDS",
    "RESEARCH_DYNAMIC_WORKER_CONTRACT_REFS",
    "RESEARCH_DYNAMIC_WORKER_REFS",
    "RESEARCH_PAPER_ANALYSIS_GRAPH_ID",
    "RESEARCH_PAPER_ANALYSIS_GRAPH_VERSION",
    "build_research_artifact_terminal_policy",
]
