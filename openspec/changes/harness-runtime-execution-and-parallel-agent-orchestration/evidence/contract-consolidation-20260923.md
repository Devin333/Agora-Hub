# Contract Consolidation Evidence

Date: 2026-09-23

The runtime contract binding is derived from existing owner constants and the
`TaskPlanSchemaRegistry`; it does not create a second schema registry or
durable store. The binding is checked at parallel dispatch admission, before
capacity admission and worker execution, and at both in-memory and durable
result acceptance paths before any result event or artifact write.

The history read path validates event and result schema ownership before
reconstructing attempt history. Cross-object checks cover plan/task identity,
plan and task checksums, worker/binding references, active attempt transitions,
dispatch group scope, and execution profile schema version.

Focused evidence:

- `.venv\\Scripts\\python.exe -m pytest tests/framework/harness/test_runtime_contract.py tests/framework/harness/task_plan/test_parallel_orchestration.py tests/framework/tool/runtime/test_tool_execution_environment.py -q` -> 58 passed.
- `.venv\\Scripts\\python.exe -m pytest tests/framework/harness/task_plan/test_attempt_history_contract.py tests/framework/harness/task_plan/test_parallel_orchestration.py -q` -> 67 passed.
- `.venv\\Scripts\\python.exe -m compileall -q framework tests/framework/harness/test_runtime_contract.py` -> passed.
- `git diff --check` -> passed.
