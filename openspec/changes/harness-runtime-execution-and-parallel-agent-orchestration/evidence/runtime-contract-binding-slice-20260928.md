# Runtime contract binding slice evidence

Date: 2026-09-28. Scope: a verified subset of tasks 1.2 and 1.3. This record
does not qualify either task as complete and does not change `tasks.md`.

## Binding result

`framework/harness/runtime_contract.py` remains a read-only cross-object
binding. It imports versions from existing owners and creates no registry,
store, serializer, or compatibility reader. The following entries were added
because each has a real writer and reader or acceptance path.

| Binding | Exact owner version | Writer and reader | Identity and checksum rule |
| --- | --- | --- | --- |
| `event_envelope` | `framework/events/canonical.py:26` `newsroom.event-envelope/v2` | `EventCandidate`/`StoredEvent` at `canonical.py:382,666`; `EventRuntime` publishes and canonical stores read them | Canonical event identity plus `content_checksum` and store-assigned `record_checksum`; `StoredEvent.from_dict` verifies both at `canonical.py:785-815` |
| `runtime_event_data` | `framework/events/runtime/projection.py:68` `newsroom.runtime-event/v1` | `RuntimeEventEnvelope` writes at `projection.py:218`; canonical publisher at `projection.py:785,836`; reducer reads at `projection_reducer.py:22` | Graph/activity identity, refs and checksums are validated before projection. `RuntimeContractBinding.current()` rejects drift from the catalog registration at `schema/catalog.py:105,777-786` |
| `budget_policy`, `budget_snapshot` | `framework/governance/budget/models.py:14` `newsroom.budget/v1` | `BudgetLedger.snapshot/restore` at `ledger.py:548,589`; production child scope is created by `CanonicalChildBudgetTrackerProvider.tracker_for` at `execution_providers.py:39,59` and checked before runner execution at `execution.py:783` | Policy digest, run/scope hierarchy, exact `GraphExecutionIdentity`, reservation operation identity, usage and settlement remain owned by `BudgetLedger`. This is distinct from TaskPlan capacity `budget_reservation` |
| `budget_event` | `framework/governance/budget/models.py:15` `newsroom.budget-event/v1` | `BudgetEvent` at `models.py:968`; canonical event catalog registration uses the same data schema | Event identity, run/scope, policy digest, ledger revision, operation/idempotency/reservation identities and amounts are owner-validated; binding rejects catalog drift |
| `subagent_attempt_identity`, `subagent_context`, `subagent_output` | `framework/harness/subagents/transcript.py:25-27` v3 schemas | `SubAgentTranscriptStorePort.write/read_context/read_output/verify` at `transcript.py:533-537`; production implementations use the same models | Exact attempt identity binds parent/child run, Graph, stage, plan, task instance, attempt and context envelope. Context/output checksums are verified by exact-field `from_dict` readers |
| `task_result` | `framework/harness/task_plan/store.py:66` `newsroom.harness-task-plan-result/v3` | In-memory and durable `append_result` at `store.py:1455` and `durable_store.py:828`; both call `validate_task_result_contract` before persistence | Result schema is now compared through `RuntimeContractBinding`; plan/stage/task/attempt/binding/output/gate refs and result checksum remain validated by the owner and acceptance boundary |
| `parent_observation` | `framework/harness/task_plan/parallel.py:84` `agora.harness-parent-observation/v1` | Coordinator writes `ParentObservation` at `parallel.py:3694`; `ParallelDispatchResult` verifies and projects it at `parallel.py:901-1017`; AgentLoop consumes the typed observation at `agent_loop/orchestration.py:685` | Group, plan version, aggregate refs/checksum and bounded task/wave summaries contribute to `observation_checksum` |
| `graph_terminal` | `framework/harness/artifacts/terminal_manifest.py:28` `newsroom.graph-terminal-manifest/v1` | Nested terminal record inside the v2 manifest; strict v1 reader is invoked by `GraphTerminalManifestV2.from_dict` | Tenant/run/Graph/version/terminal state/artifact refs are included in the nested terminal checksum projection |
| `artifact_manifest` | `framework/harness/artifacts/terminal_manifest.py:29` `newsroom.graph-terminal-manifest/v2` | Production construction at `interfaces/services/agent_loop_smoke_service.py:536`; storage writer/reader at `infrastructure/storage/artifacts/graph_terminal.py:56,189`; Research adapter reads/writes at `infrastructure/research/artifact_port.py:1184-1196` | V2 binds the v1 terminal record to execution versions and `manifest_hash`; `parse_graph_terminal_manifest` rejects v1 or historical manifests at `terminal_manifest.py:1043-1070` |
| `side_effect_intent`, `side_effect_decision`, `side_effect_outcome` | `framework/harness/side_effects/models.py:22-24` v2/v2/v3 | Harness side-effect ports accept the typed models; exact readers are `from_dict` at `models.py:291,564,1151` | Graph/run/attempt, identity and subject scopes, idempotency, fencing and outcome refs are covered by each owner checksum |
| `tool_result_envelope`, `tool_side_effect_receipt` | `framework/tool/models/result_envelope.py:26-27` `@1` schemas | Tool runtime writes `ToolResultEnvelope`; `ToolSideEffectReceipt.from_dict` verifies exact fields and receipt checksum at `result_envelope.py:45-191` | Tool/call/attempt/idempotency/operation/gate/response identity and determinate state are bound by the receipt checksum |
| `harness_bound_tool_receipt`, `tool_side_effect_evidence` | `framework/harness/runtime/tool_result_adapter.py:61-64` `@1` schemas | Adapter writes at `tool_result_adapter.py:271-316,580-636`; verifier reads at `tool_result_adapter.py:114-171` | Exact Graph `NodeResultBinding`, underlying tool receipt checksum, response checksum and bound receipt checksum are verified before evidence acceptance |

Existing bindings for execution profile, child handle, TaskPlan/group/wave,
transcript/receipt/bundle, capacity reservation, attempt history and parent
continuation remain sourced from their existing owners.

## Boundary checks added

- `RuntimeContractBinding.current()` compares the runtime-event and budget-event
  catalog registrations with their model owners and raises
  `RUNTIME_CONTRACT_OWNER_DRIFT` on disagreement.
- Task result acceptance and history reads compare the incoming schema with
  `binding.owners["task_result"]`; an unknown schema is rejected before replay.
- `projection_reducer.py` exports the runtime event version from
  `projection.py` rather than defining another constant. Its reducer body must
  retain an audited string constant because replay registration forbids
  `LOAD_GLOBAL` and requires exactly `(state, event)`. The boundary test feeds
  the owner schema through the registered reducer and requires a projected
  status, so changing that audited literal independently fails the test.

## Receipt version findings

- `ExecutionReceipt` (`framework/execution_environment/models.py:959`) has no
  `schema_version`. Its operator projection is checksummed, but there is no
  standalone serialized reader or exact version constant to bind. This slice
  does not invent an execution-receipt schema. This is not automatically a
  contract defect: the current object is an in-memory provider result with a
  checksummed operator projection. If a future task persists it as a standalone
  contract, that owner must define the version and historical reader policy.
- `ChildAgentTerminalReceipt` (`framework/harness/subagents/supervisor.py:522`)
  also has no standalone version. Recovery reconstructs it and verifies the
  canonical receipt checksum at `supervisor.py:2062-2069`; the durable wrapper
  is `newsroom.harness-child-lifecycle-state/v2` at
  `supervisor_store.py:29,466`, and the history reader validates admission
  identity and legal terminal/close transitions at `supervisor_store.py:670-718`.
  Therefore the current version strategy is the outer lifecycle-state version
  plus exact receipt fields/checksum and transition validation. That is a
  coherent owner strategy for current history. Any incompatible receipt change
  must bump the outer owner or introduce an explicit receipt schema with a
  deliberate reader; relabeling existing history is not valid.

## Unmet acceptance facts

- Task 4.3 and task 5.1 remain open: this slice pins SubAgent and result schema
  owners but does not prove the complete production child path verifies every
  transcript/output/artifact/tool receipt, memory/budget and gate reference.
- Tasks 5.2 and 5.3 remain open: parent observation has a typed live
  writer/consumer and checksum, but durable `PENDING` submission,
  same-parent-turn redelivery and restart reuse are not implemented or tested
  by this slice.
- Tasks 3.1-3.3 remain open: runtime event owner drift is now detected, but the
  complete event routing, checkpoint resume, offline rebuild and identity/
  redaction/replay test matrix is outside this slice.
- Task 2.6 remains open: exact execution profile/receipt ownership in local code
  is not provider deployment capability or rollback qualification.
- Tasks 7.1 and 7.2 remain open: focused tests below pass, but the integrating
  agent still owns the complete architecture, full test, smoke and release
  prerequisite gates. Consequently tasks 1.2 and 1.3 remain unchecked.

## Verification

- `python -m pytest tests/framework/harness/test_runtime_contract.py tests/framework/events/test_runtime_projection_reducer.py tests/framework/events/test_runtime_event_projection.py tests/framework/harness/task_plan/test_durable_task_plan_store.py tests/framework/governance/budget/test_models.py tests/framework/governance/budget/test_replay.py tests/framework/llm/budget/test_tracker.py tests/framework/harness/runtime/test_tool_result_adapter.py`: 122 passed.
- `python -m scripts.dev compile`: passed.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`: passed.
- `git diff --check`: passed; Git reported line-ending conversion warnings only.
- Full smoke was not run in this worker slice; the integrating agent owns the
  required pre-commit smoke over the combined worktree.

Integrating pre-commit smoke on the combined worktree passed: 3744 passed, 23 deselected; AgentLoop offline run and source validation succeeded. This does not close the unmet cross-boundary acceptance requirements above.

## Subsequent receipt owner reconciliation

The receipt findings and 1.2/1.3 qualification status above describe this
historical slice, not a permanent constraint. The later
`runtime-receipt-owner-reconciliation-20260928.md` records
`ExecutionReceipt` v1, its exact owner reader and provider registry acceptance,
plus the consolidated child terminal receipt reader. Consult that later record
and current `tasks.md` for the latest task status; task 1.3's complete runtime
invariant matrix remains a separate acceptance decision.
