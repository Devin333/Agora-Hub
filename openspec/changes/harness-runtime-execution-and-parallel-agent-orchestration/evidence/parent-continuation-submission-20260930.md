# Parent Submission And Continuation Evidence (2026-09-30)

The AgentLoop orchestration boundary now persists a canonical candidate
submission before stage execution. `CandidateDedupIdentity` binds the run,
stage, parent turn, and action correlation; `CandidateSubmission` derives a
stable submission and plan identity while retaining the writer admission id
and record checksum. Repeated requests with the same identity reuse the
durable candidate and terminal result without materializing a new candidate,
worker invocation, plan, group, or event. A conflicting candidate is rejected
before input resolution and preserves the original submission diagnostic.

`AgentSubmissionReceipt` is returned for first admission, pending recovery,
rejection, and terminal redelivery. A candidate committed before plan
acceptance returns a bounded `PENDING` receipt and can be resumed explicitly by
the Harness recovery ingress. Capacity waits remain pending and preserve their
original deadline and durable waiting event. The receipt and its nested
submission identity, plan identity, and checksums are independently validated
on serialization and redelivery.

Parent observations are projected from checksum-validated durable group join
facts. `ParentContinuation` carries only the observation identity, version,
checksum, parent scope, and submission identity; it never copies child prompt
or private context. Pending-to-delivered state transitions retain the same
observation version and checksum. Replayed continuations are idempotent, and
reopened stores recover the terminal continuation without invoking workers.
Two concurrent continuation writers use the same sequence CAS. A stale writer
accepts an already committed terminal transition when identity, observation
checksum, parent scope, and submission identity all match; unrelated or
conflicting content still fails closed.

Focused verification:

* `python -m pytest -q tests/framework/harness/agent_loop/test_orchestration_submission.py::test_continuation_sequence_race_accepts_terminal_advance`
  (`2 passed`; memory and durable store variants).
* Existing submission, continuation contract, replay, restart, redelivery, and
  no-worker-reinvocation tests pass in the focused orchestration suite.

This evidence covers the generic Harness AgentLoop submission boundary. Full
parallel coordinator qualification, dynamic Research golden parity, and final
release-gate qualification remain open and are intentionally not claimed here.
