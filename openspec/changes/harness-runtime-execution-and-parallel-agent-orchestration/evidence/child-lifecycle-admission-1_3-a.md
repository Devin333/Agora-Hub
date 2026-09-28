# Child lifecycle admission and transition validation, task 1.3 slice A

This evidence qualifies the existing Harness-owned child lifecycle acceptance path against its original durable admission. Task 1.3 remains open: TaskPlan dispatch/result acceptance, retained evidence readback, and the remaining transition/event boundaries need separate verification. It does not close event-routing task 3.1.

## Trusted admission and real owner

`ChildAgentSupervisor` writes the original `child_spawned` handle through the child lifecycle owner. Recovery and the transactional lifecycle projector now reuse `_validate_event_admission_binding` to validate subsequent events against that original handle, not against identities repeated in the candidate event. The validator checks the canonical event/receipt/handle owner contracts and binding for operation, run, tenant, child, parent and child Graph identities, stage/task/attempt, authorized tool and memory scopes, budget, transcript, and creation identity. Historical markerless lifecycle-state/v2 events remain readable under their existing reader policy; marker-rich events must satisfy the versioned contract.

Before a lifecycle transition changes the transactional state snapshot, reservation, or capacity, the projector validates its admission binding. A `CANCELLED` terminal also requires an earlier durable `child_cancel_requested` event. Rejected candidates map to the existing typed `child_lifecycle_conflict` diagnostic; persisted corrupt history still fails closed as `child_event_store_corrupt`. Existing successful terminal, cancel, and recovery behavior remains intact.

## Physical storage and negative evidence

The SQLite-backed lifecycle tests submit a self-consistent, rechecksummed terminal event and nested receipt whose parent Graph identity differs from the original admitted handle while run, tenant, child, and operation remain unchanged. The event is rejected before the compare-and-swap commit; the exact `TransactionalStateSnapshot`, durable event history, reservation/capacity, and worker invocation count remain unchanged. A second test submits a valid production cancellation terminal without its prior durable cancellation request and observes the same rejection and preserved business state.

The positive tests reopen markerless v2 history from SQLite, recover its existing operation, and read its terminal result. They also verify the legal spawn -> heartbeat -> cancel request -> terminal -> close history and its `CANCELLED` outcome. These tests exercise the real lifecycle owner and recovery reader, not only a standalone schema helper.

`ChildAgentSupervisor._emit` currently publishes a canonical runtime event before appending to the separate child lifecycle sink. This slice does not establish an atomic transaction across these two owners; a lifecycle rejection is not evidence that the canonical runtime stream high-watermark is unchanged. Event ordering and cross-owner reconciliation remain to be qualified under task 3.1.

## Verification

- Focused child supervisor/runtime/contract suite: 94 passed.
- Focused TaskPlan history/spawn/recovery/parallel suite: 117 passed.
- `python -m scripts.dev smoke`: 3768 passed, 23 deselected; AgentLoop smoke succeeded with zero network calls, and source validation returned zero errors and zero warnings.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict` and `git diff --check`: passed on this slice.

Task 1.3 remains unchecked until its remaining identity, scope, reference, capability, policy, schema, and transition matrix is verified at the actual TaskPlan result and event/continuation boundaries.
