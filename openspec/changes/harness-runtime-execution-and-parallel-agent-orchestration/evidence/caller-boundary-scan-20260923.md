# Harness execution and child caller boundary scan — 2026-09-23

Task 2.5 is qualified by executable architecture scans over the production Python roots `backend`, `framework`, `infrastructure`, and `interfaces`.

## Proved boundaries

- Every production construction of `ToolExecutor`, `ToolBatchExecutor`, and `ResearchParserExecutionAdapter` is frozen by exact path and constructor identity. Harness-managed constructors receive `execution_environment`; the composition factory owns the only `**kwargs` injection, and `framework/tool/inspection/testing.py` remains the inventory-approved deterministic diagnostic exemption.
- Physical execution dispatch is restricted to the execution registry provider boundary, `ToolExecutor` through that registry, and `ResearchParserExecutionAdapter` through its injected registry. A new caller fails the architecture scan until explicitly classified and wired.
- `ChildAgentSupervisor` is constructed only by `HarnessOwnedChildAgentRuntime`. Dynamic Research creates the owned runtime lazily from the canonical durable state ports. The scan verifies that its nested `execute` callback calls `subagent_adapter.invoke`, while the enclosing `ResearchAnalysisTaskPlanStageWorker` receives that callback only as `worker_executor` and receives the exact `child_runtime_binding.parallel_coordinator` and `child_runtime_binding.supervisor` for physical dispatch. A separate scan verifies that `SubAgentRuntime` accepts no supervisor/coordinator, imports neither lifecycle type, and calls none of the lifecycle methods. It remains a candidate/transcript adapter and does not own child dispatch.
- Production child `spawn`, `status`, `wait`, `cancel`, and `close` calls are restricted to `framework/harness/task_plan/parallel.py`; owner start/recovery/shutdown calls are restricted to `framework/harness/subagents/owned_runtime.py` and `interfaces/composition/research_child_runtime.py`.

## Retained blocked path

`backend/research/document/pdf_compiler.py:_run_nougat` is not claimed as migrated. With no injected command runner it raises `ExecutionEnvironmentUnavailableError`, and the architecture scan proves that function contains no `subprocess` call. The caller inventory therefore retains `nougat-direct-process` as `blocked` / `follow-up-required`. This is fail-closed evidence, not production qualification for Nougat.

The prior `research-subagent-runtime` inventory entry is now migrated because the child lifecycle owner landed after the original inventory was written. Its proof points to the new architecture scan. This status applies to child lifecycle dispatch only: `research-canonical-events` remains `blocked` / `follow-up-required` under the runtime-event-transport owner, and task 2.5 does not claim that event authority has migrated.

## Verification

The qualification commands and final commit SHA are recorded by Git history for this evidence file. Required gates are:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/architecture/test_harness_execution_and_child_caller_boundaries.py tests/architecture/test_production_caller_inventory.py tests/interfaces/composition/test_research_child_runtime.py -q
.\.venv\Scripts\python.exe -m scripts.dev compile
git diff --check
openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict
.\.venv\Scripts\python.exe -m scripts.dev smoke
```
