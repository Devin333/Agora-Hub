## Why

Research already has useful pieces for input classification, claim review, candidate retrieval planning, and RAG evaluation, but seven overlapping proposals describe different versions of the same trust boundary. Implementing them independently would duplicate state, evaluation artifacts, and control-plane behavior before the primary product risk is measured: whether a paper answer preserves the user's constraints and whether its important claims are actually supported by the cited source.

This change consolidates the retained value into a staged, evidence-first delivery. It establishes measurable correctness improvements before optional retrieval, context-window, or progress optimizations are enabled.

## What Changes

- Add one Research quality foundation that extends existing evaluation tooling with fixed comparison cases, deterministic contract checks, and release evidence. It does not introduce a second evaluation platform, run store, or promotion system.
- Integrate the existing Research input contract into the current dynamic AgentLoop path. The integration preserves one accepted current task, partitions material by source and status, validates the final request, and fails closed or uses the existing path when an opted-in profile is incomplete.
- Add a narrow evidence-verification core for one high-value claim type. It binds a claim revision to exact source locators, conditions, and typed observations; deterministic resolution controls eligibility. LLMs and reviewers remain candidate-observation producers and do not authorize routing or publication.
- Add an opt-in, default-off retrieval-candidate experiment that uses the existing `WorkerRAGPlanner`. Original queries remain executable, workers can only propose bounded candidates, deterministic validation protects hard constraints, and rollout requires measured improvement over the deterministic baseline.
- Define a trigger-gated, single-run context-continuity slice. It may be implemented only after traces show context loss, condition drift, or repeated-read cost; it reassembles accepted evidence and conditions at a real window boundary without a parallel archive, event store, or claim-state system.
- Keep progress governance in observation-only form: stable reason codes and metrics may be recorded, but this change does not add an enforce-mode stop policy.
- Retire the seven superseded Research proposals and their duplicate task/spec artifacts in the same documentation migration. Retire obsolete Workflow-era change directories that are replaced by the canonical Graph-only contracts; retain current Graph deployment, replay, and rollback requirements under their active owners.

## Capabilities

### New Capabilities
- `research-evidence-context-quality`: Research-owned input integrity, evidence verification, deterministic eligibility, and evaluation requirements for an explicitly enabled paper-analysis path.
- `research-retrieval-candidate-experiment`: Bounded, validated query-candidate experiments that preserve the deterministic retrieval baseline.
- `research-context-continuity`: Trigger-gated, single-run reassembly of accepted research state at a real model-context boundary.

### Modified Capabilities
- `paper-rag-retrieval-query-planner`: Allows an explicitly enabled, validated candidate source while retaining deterministic planning and fallback behavior.
- `research-runtime`: Requires opted-in Research quality paths to use the application boundary and Harness-controlled deterministic eligibility before report publication.

## Impact

- **Research domain and application:** `backend/research/domain/input_contract.py`, claim review domain objects, `ClaimReviewResolver`, and the single-paper/Paper RAG application path gain a single owned integration boundary.
- **Framework:** `PromptBuilder` receives structured, Research-prepared input only at the opted-in dynamic AgentLoop boundary. Harness remains the sole owner of planning, execution, verification, budgets, and publication.
- **RAG:** `WorkerRAGPlanner` remains candidate-only; the existing deterministic planner and retrieval channels remain the default path.
- **Evaluation and CI:** existing `backend/research/rag/evaluation` components and current fixtures are extended rather than replaced.
- **Documentation:** replaces seven proposed Research changes and removes obsolete Workflow-era change directories. Existing active changes for deployment qualification, durable events, LLM context admission, and workspace acceptance remain separately owned.
