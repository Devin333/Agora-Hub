# Runtime projection checkpoint and replay evidence

Date: 2026-09-28. Change: `harness-runtime-execution-and-parallel-agent-orchestration`.
This evidence closes tasks 3.2 and 3.3 for the durable runtime projection.
Task 3.1 remains open because canonical routing coverage for all turn, tool,
approval, context, worker, and child lifecycle producers is not complete.

## Checkpoint resume and offline rebuild

`test_sqlite_runtime_replay_resumes_after_reopen_and_matches_full_rebuild`
uses the real SQLite event runtime and replay checkpoint store. It first saves a
two-event prefix, then discards the original composition and reopens the same
SQLite storage. After appending a runtime tail event and a foreign-schema event,
it resumes from sequence 3 using the durable prefix checkpoint.

The test verifies that repeating the same replay ID and request returns the
same report and checkpoint, and that the resumed checkpoint state exactly
matches a full rebuild. The result checksum and replay history checksum also
match the full rebuild. It verifies the stream cursor reaches the canonical
sequence 4 while only the runtime event changes projected status. During both
resumed and full offline replay, `event_runtime.publish` is replaced by a
fail-fast function, so replay cannot publish live events.

## Identity, redaction, cursor, checksum, and routing boundaries

`test_identity_collision_is_rejected` continues to reject changed event
content. The parameterized
`test_identity_collision_rejects_explicit_stream_and_sequence_conflicts` also
checks that a repeated event ID with an explicitly different `stream_id` or
`sequence` raises `RuntimeEventIdentityConflict`. The in-memory idempotency path
inherits canonical stream and sequence only when the retry omits them; it no
longer overwrites explicit retry identity before comparison.

`test_redacted_runtime_metadata_is_accepted_by_durable_event_runtime` verifies
that nested API keys and protected argument content are redacted before the
event is persisted. Reducer tests validate stream scope, cursor sequence,
checkpoint schema, and identity shape, and reject cursor tampering. The SQLite
rebuild test verifies deterministic state, result checksum, and history
checksum across resume and full replay.

`test_runtime_reducer_does_not_promote_worker_routing_candidates` supplies a
worker status payload containing routing, next-action, and tool-dispatch
candidates, then checks that the projection contains only its fixed status and
cursor schema. The integration test
`test_runtime_projection_reducer_has_no_live_execution_dependency` also
checks that the reducer has no worker, tool, LLM, scheduler, or executor
references. Together these tests keep replayed worker metadata out of runtime
routing authority.

## Verification

- `python -m pytest tests/framework/events/test_runtime_projection_reducer.py tests/framework/events/test_runtime_event_projection.py tests/interfaces/services/test_runtime_replay_sqlite_integration.py -q`: 26 passed.
- `python -m scripts.dev compile`: passed.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`: passed.
- `git diff --check`: passed.
- `python -m scripts.dev smoke`: passed; 3,755 tests passed and 23 were deselected. The AgentLoop CLI smoke succeeded with zero network calls, and `sources validate` reported `is_valid=true`, `error_count=0`, `warning_count=0`.
