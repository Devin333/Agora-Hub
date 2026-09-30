# Canonical runtime event tenant scope

Date: 2026-09-30. Change: `harness-runtime-execution-and-parallel-agent-orchestration`.

This slice binds the configured `CanonicalRuntimeEventPublisher` to the
durable tenant scope selected by the composition root. `runtime_event_publish_request`
now carries that scope into the canonical `EventPublishRequest`, and the
Research composition binds its existing opaque scope `research-runtime`.

The publisher keeps one immutable scope for its lifetime and rejects blank or
non-text tenant values. A SQLite projection test verifies that the stored
event is readable only from the bound tenant stream and retains the same
tenant on the canonical envelope.

This does not qualify generic AgentLoop event routing, all producer families,
or parent continuation delivery. Tasks 3.1, 5.2, and 5.3 remain open.

## Checks

- `python -m pytest -q tests/framework/events/test_runtime_event_projection.py tests/interfaces/composition/test_research_child_runtime.py`: passed.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`: passed after the change.
