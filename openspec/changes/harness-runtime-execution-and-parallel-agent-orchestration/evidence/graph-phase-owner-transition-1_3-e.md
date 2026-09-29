# Durable Graph phase transition owner: task 1.3 evidence slice

Date: 2026-09-29. Change: `harness-runtime-execution-and-parallel-agent-orchestration`.
Scope: require live Graph phase writes and offline recovery to obey the same
canonical identity and phase-history rules. Task 1.3 and task 3.1 remain open;
this slice does not qualify all contract validators or lifecycle event producers.

## Defect and owner boundary

A checksum-valid `graph_phase_transition_recorded` envelope previously proved
its own serialization but not whether the accepted Graph contained its node
instance and current attempt, or whether the transition followed the node's
prior durable phase. A forged transition could enter the canonical event stream
and survive self-integrity checks without a matching trusted Graph history.

`DurableHarnessTransitionPort` now admits a new phase transition against the
recovered Graph/state and the canonical stream head before publishing. It
checks the Graph/run/node/stage identity, executable node, current attempt,
trusted node status, legal entry/exit predecessor, and terminal attempt. The
event adapter validates exact stored envelope/context fields, while recovery
applies the same phase rules to each canonical event in order. Identical event
retries are idempotent; an identity collision, stale head, corrupted stored
record, or illegal transition fails closed. An unowned `DurableHarnessEventPort`
can no longer append phase transitions without the Graph transition owner.

## Executed checks

- SQLite-backed live tests reject a missing/trusted-different node, mismatched
  Graph identity or attempt, invalid phase predecessor, and terminal reentry
  without publishing a new event.
- SQLite reopen rejects checksum-recomputed corrupt phase envelopes or wrong
  sequence and verifies that offline recovery makes no live publish call.
- Ordered entry/exit, interleaved canonical events, valid failed-attempt retry,
  and an identical event retry remain replayable and idempotent.
- Focused Graph phase and adjacent tests passed (32 tests); `python -m
  scripts.dev compile`, strict OpenSpec validation, and cached diff check passed.
- Required `python -m scripts.dev smoke` passed: 3790 passed, 23 deselected,
  31 warnings in its test phase (3467.28s). The AgentLoop smoke succeeded and
  `sources validate` returned `is_valid=true`, zero errors/warnings.

This slice does not prove complete task 1.3 identity/capability/policy/reference/
schema validation across owners or task 3.1 turn, tool, approval, context,
worker, and child producer routing. Those acceptance tasks stay unchecked.
