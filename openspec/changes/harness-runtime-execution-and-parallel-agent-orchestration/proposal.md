## Why

The repository currently has separate OpenSpec changes for Harness execution safety and parallel sub-agent orchestration. Their contracts overlap at the exact boundary that determines whether a child can be admitted, isolated, observed, recovered, verified, and joined. Keeping them separate leaves the production path split between two task lists and makes it possible to qualify scheduling without proving execution authority, or qualify sandboxing without proving durable parallel continuation.

This change creates one bounded delivery contract for the runtime prerequisites and the parallel AgentLoop path. It preserves the existing Harness authority model, keeps the static Research path as the default, and makes dynamic Research opt-in only after deterministic gates and release evidence pass.

## What Changes

- **BREAKING**: Make the merged runtime contract the single owner for sandboxed execution, child-agent lifecycle, canonical runtime events, parallel group/wave admission, and durable parent continuation.
- Complete the `ExecutionEnvironment` contract and provider capability checks for filesystem, network, environment, process-tree cancellation, timeout, and termination confirmation.
- Complete Harness-owned `ChildAgentSupervisor` lifecycle, lease/recovery, cancellation, indeterminate handling, and no-duplicate-side-effect guarantees.
- Project turn, tool, approval, context, worker, and child-agent facts through the canonical durable event stream with redaction, identity, cursor, and replay guarantees.
- Route parallel children through real `SubAgentRuntime`/`AgentRunner` and ToolRuntime composition with isolated refs, budgets, leases, receipts, and deterministic result verification.
- Implement bounded group/wave join, multi-pool capacity, retry, replacement replan, fail-fast cancellation, reclaim, quarantine, and completion-order-independent aggregation.
- Implement durable `PENDING` submission and same-parent-turn continuation with idempotent redelivery and terminal checksum reuse across restart.
- Complete the dynamic Research fan-out path for structure, contribution, and experiment branches while preserving evidence, quality-gate, reader, card, artifact, and publication boundaries.
- Add integration, architecture, replay, crash, recovery, golden-parity, rollout, and rollback evidence. Keep feature default-off and fail closed when required dependencies or deployment capabilities are unavailable.
- Remove the two superseded standalone change directories after this merged change is validated; historical provenance remains in Git history.

## Capabilities

### New Capabilities

- `harness-execution-and-parallel-agent-runtime`: Defines the merged execution-environment, child supervision, runtime-event, parallel orchestration, parent continuation, Research fan-out, and release-gate contract.

### Modified Capabilities

The merged capability consumes the existing Harness, AgentLoop, TaskPlan, and Research contracts. Their existing requirements remain authoritative; this change adds one cross-cutting capability that binds them into a single runtime and release contract.

## Impact

- Framework code under `framework/execution_environment`, `framework/harness`, `framework/agent`, `framework/tool`, and `framework/events`.
- Research graph/application composition under `backend/research` and `interfaces/composition`.
- Existing durable event, checkpoint, artifact, budget, tool receipt, and supervisor ports; no new authority path or compatibility runtime.
- Integration and architecture tests, Research golden fixtures, rollout telemetry, deployment capability evidence, and rollback/replay records.
- The static Research execution path remains unchanged when the merged parallel feature is disabled or dependencies are unavailable.
