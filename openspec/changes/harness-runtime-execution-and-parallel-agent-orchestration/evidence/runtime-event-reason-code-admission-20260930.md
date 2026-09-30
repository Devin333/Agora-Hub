# Runtime Event Reason-Code Admission Evidence (2026-09-30)

The canonical runtime event envelope previously allowed lifecycle facts such
as timeout, cancellation, indeterminate, child terminal, execution terminal,
turn abort, approval decision, and runtime error to omit `reason_code`. That
made a durable event technically replayable but not diagnostically attributable
to a bounded control outcome.

`RuntimeEventEnvelope` now requires a bounded diagnostic code for those event
types. Codes are limited to 128 characters using the canonical identifier
alphabet (`A-Z`, `a-z`, digits, `.`, `_`, `:`, `-`). `RuntimeEventEmitter`
assigns the canonical event type as a deterministic fallback when a producer
omits a reason, preserving existing producer paths while ensuring the stored
fact is explainable. Existing explicit reason codes remain unchanged.

Focused verification:

* `python -m pytest -q tests/framework/events/test_runtime_event_projection.py`
  (`35 passed`).
* `python -m pytest -q tests/framework/events/test_runtime_projection_rebuild_atomic.py tests/interfaces/services/test_runtime_replay_sqlite_integration.py tests/interfaces/api/test_runtime_operator_status_api.py`
  (`10 passed`).
* Tests cover missing and malformed reason codes, deterministic emitter
  fallback, redaction, identity conflicts, projection replay, SQLite storage,
  and read-only operator APIs.

This closes the local reason-code invariant for the canonical runtime event
owner. Full producer-family coverage for every turn/tool/approval/context/
worker/child path and external deployment qualification remain open under task
3.1 and the release gates.
