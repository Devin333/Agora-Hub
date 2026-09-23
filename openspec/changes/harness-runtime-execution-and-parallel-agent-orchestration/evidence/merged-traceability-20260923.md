# Merged Requirement Traceability

## Frozen baseline

- Audit date: 2026-09-23.
- Source snapshot: `b13414cc2b64216c53fb4cb7f2ae76fb703a23ad`, the parent of migration commit `1f4a8ae2`.
- Code HEAD before this qualification repair: `63ae01b9feb7cfc24863c4fca7fd296de1044f9b`.
- Both predecessor directories were removed by the migration commit. All original proposal/design/spec/tasks/evidence files remain addressable with `git show <source-snapshot>:<original-path>`.
- Original runtime evidence: `openspec/changes/harness-runtime-execution-safety/evidence/runtime-execution-safety.md`.
- Original parallel evidence: `openspec/changes/harness-codex-style-parallel-agent-orchestration/verification.md`, `task-1-10-verification.md`, `task-2-2-verification.md`, `task-2-3-verification.md`, and `task-2-5-verification.md`.
- Historical checks were 23/28 and 23/46 respectively. They describe their original scope; merged cross-boundary acceptance must be supported by current evidence.

## Unchecked predecessor tasks

Each row maps an unchecked task in the frozen source to its current owner. No row is considered implemented merely because it has been migrated.

| Predecessor | Original task | Current merged task / retained owner |
| --- | --- | --- |
| Runtime safety | 1.1 context preflight closure | 1.1 dependency inventory and 7.2 release prerequisite; `model-aware-llm-context-preflight` 7.1-7.6 remain owned there |
| Runtime safety | 5.1 restart, timeout, loss, cancellation, external effects | 2.2-2.4 |
| Runtime safety | 5.2 caller/architecture scan | 2.5 |
| Runtime safety | 5.3 focused/compile/smoke/strict checks | 7.1-7.2 |
| Runtime safety | 5.4 provider deployment/rollback | 2.6, 7.6 |
| Parallel orchestration | 2.6 real child runtime/tools | 4.3 |
| Parallel orchestration | 2.7 result verification | 5.1 |
| Parallel orchestration | 2.9 multi-wave join/aggregate | 4.5 |
| Parallel orchestration | 2.10 retry/replacement/quarantine | 4.6 |
| Parallel orchestration | 2.11 fail-fast/cancel/reclaim/fence | 4.6 |
| Parallel orchestration | 2.13 overlap/capacity/recovery proofs | 4.7 |
| Parallel orchestration | 3.2 durable submission/PENDING | 5.2 |
| Parallel orchestration | 3.3 parent continuation/redelivery | 5.3 |
| Parallel orchestration | 3.4 bounded verified summaries | 5.4 |
| Parallel orchestration | 3.5 generic production dependencies | 1.4, 4.3, 5.6, 7.4 |
| Parallel orchestration | 3.6 legacy single-child parity | 5.5 |
| Parallel orchestration | 3.7 parent contract tests | 5.6 |
| Parallel orchestration | 4.3 Research role fan-out | 6.1 |
| Parallel orchestration | 4.6 Research negative dependencies | 6.6 |
| Parallel orchestration | 4.7 golden parity | 6.4 |
| Parallel orchestration | 4.8 full Research failure/recovery histories | 6.5 |
| Parallel orchestration | 5.1 complete validation | 7.1-7.2 |
| Parallel orchestration | 5.2 explicit composition states | 1.4 |
| Parallel orchestration | 5.3 telemetry evidence | 7.3 |
| Parallel orchestration | 5.4 allowlisted AgentLoop | 7.4 |
| Parallel orchestration | 5.5 allowlisted Research | 7.5 |
| Parallel orchestration | 5.6 rollback rehearsal | 7.6 |
| Parallel orchestration | 5.7 independent release evidence | 7.7 |

## Retained dependency blockers

The source runtime checklist also recorded `durable-event-runtime` 9.5 and `harness-workflow-graph-runtime` 1.1 as external production qualification blockers. Both remain unchecked in the current tree. The former requires real deployment, rollback, and independent governance evidence; local tests cannot generate those facts. The latter requires canonical event/replay, attempt termination/idempotency/fencing, and side-effect authority contracts.

`model-aware-llm-context-preflight` 7.1-7.6 are also currently unchecked. This merged change retains that dependency instead of copying or completing its task list. Release task 7.2 requires checking these outstanding prerequisites as well as running local validation.

## Production caller inventory

Read-only scan used:

```powershell
rg -n 'subprocess\.(Popen|run|call|check_call|check_output)|create_subprocess|os\.system|ChildAgentSupervisor\(' framework infrastructure backend interfaces --glob '*.py'
```

| Surface | Current entry | Observation |
| --- | --- | --- |
| Physical subprocess execution | `infrastructure/execution_environment/docker.py:115`, `:300` | Provider-owned `Popen` wait and subprocess command runner |
| Sandboxed tools | `framework/tool/runtime/executor.py:784`, `:961` | Explicit execution profile admission and `registry.execute(request)` |
| Provider composition | `interfaces/composition/runtime_execution.py` | Docker provider registration/capability diagnostics |
| Dynamic Research children | `interfaces/composition/research.py:1607` | Shared process-scoped supervisor; durable lifecycle log not yet injected |
| PDF compiler | `backend/research/document/pdf_compiler.py:1230` | Scan hit is a comment rejecting host subprocess fallback, not a call |
| Child durable storage | `framework/harness/subagents/supervisor_store.py` | CAS-backed lifecycle store exists; production recovery wiring remains open |

This is a frozen caller inventory, not proof that every call path meets the full 2.5 contract. Scoped production integration, active cancellation, and independent process recovery evidence are still required.

## Exact local versions

| Dependency | Version |
| --- | --- |
| Python | 3.12.7, Anaconda, MSC v.1929 64-bit |
| SQLite | 3.45.3 |
| pydantic | 2.12.5 |
| pytest | 9.0.3 |
| jsonschema | 4.23.0 |
| PyYAML | 6.0.1 |
| fastapi | 0.135.3 |
| httpx | 0.27.0 |
| psycopg | 3.3.4 |
| psycopg-pool | 3.3.1 |
| OpenSpec CLI | 1.3.1 |
| Docker Desktop | 4.72.0 |
| Docker Engine | 29.4.2, Linux amd64 |
| Local test image | `redis:7-alpine`, resolved ID `sha256:9de71018be8413f419688ac34e19dbac432d1c6d9593d2ded99a82eebefeb18f` |

## Worktree at qualification start

At smoke start, the code changes against the frozen HEAD were limited to supervisor cancellation identity, Docker cleanup handling, their tests, and the spawn recovery test synchronization. The new offline cleanup test was untracked. Integration fixture corrections made during smoke affected only Docker tests outside the smoke selection and were separately validated with the real daemon; production code was held stable.

OpenSpec edits correct evidence boundaries, the input-contract premature checkbox, migration wording, and this traceability record. `outputs/` was already untracked and is excluded from staging. The new restart recovery evidence and this record are within the ignored OpenSpec directory and require explicit path-scoped staging.

Current results are recorded in `docker-qualification-20260923.md`, `restart-recovery-20260923.md`, and `qualification-20260922.md`. This inventory task does not qualify provider rollout, parent continuation, production restart, Research parity, or rollback.
