# Canonical runtime event routing slice

Date: 2026-09-28. Change: `harness-runtime-execution-and-parallel-agent-orchestration`.
This record qualifies a production routing slice of task 3.1. Task 3.1 remains
open because turn, tool, approval, context, worker, and all child producers still
need complete end-to-end coverage and the remaining projection/replay contracts
are tracked by tasks 3.2 and 3.3.

## Single durable owner in dynamic Research

The configured Research composition creates one `CanonicalRuntimeEventPublisher`
for the run's durable `EventRuntime`. The same publisher is injected into both
the dynamic `LazyResearchChildRuntime` and the dynamic `SubAgentRuntime`.
`LazyResearchChildRuntime` passes it through `HarnessOwnedChildAgentRuntime` to
`ChildAgentSupervisor`; child lifecycle facts therefore use the canonical event
publisher instead of a child-only state log as their only runtime record.

The publisher reuses `RuntimeEventEmitter` and the existing `EventRuntime` port.
That keeps event schema/version ownership, identity checks, bounded references,
metadata redaction, and reason-code normalization in the existing runtime event
boundary. No second event store or routing authority was introduced.

## Durable lifecycle and restart evidence

The focused integration test uses a real SQLite event runtime. It admits a child,
waits for a terminal result, reads the canonical stream, and verifies that
`child_spawned` and `child_terminal` are stored as
`newsroom.runtime-event/v1` events. It then closes and reconstructs the runtime,
recovers the committed child operation as `SUCCEEDED`, and verifies that replayed
recovery does not append a second `child_spawned` event.

This proves the dynamic child lifecycle path is durable and idempotent for the
covered spawn/terminal/restart case. It does not qualify every event producer,
parent continuation, projection checkpoint, offline rebuild, or Research golden
history; those remain open under tasks 3.1-3.3, 4.3, 5.1-5.3, and 6.1-6.6.

## Checks

- Focused child runtime, supervisor, and composition suite: 61 passed.
- `python -m scripts.dev compile`: passed.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`: passed.
- `git diff --check`: passed before commit.
