# Parent Observation Projection Checksum Evidence (2026-09-30)

The generic AgentLoop parent observation is a derived view: it applies policy
limits, redacts sensitive values, and truncates summaries and diagnostics before
the view enters the parent's next model request. Previously this final view
carried no checksum, so its exact serialized content could not be distinguished
from the checksum-bound durable group observation from which it was projected.

`ParentObservation.project()` now reserves a checksum field while fitting the
configured UTF-8 byte budget, completes redaction and deterministic truncation,
then computes `observation_checksum` over every remaining projected field. The
digest therefore binds the actual parent-visible view, and checksum overhead
is included in `max_observation_bytes`. Existing checksum-bound result refs,
schema version, and truncation behavior remain in the view.

Focused verification:

* `python -m pytest -q tests/framework/agent/loop/test_delegate_batch_orchestration.py`
  (`45 passed`).
* Tests verify checksum stability against the final projected payload,
  redaction before checksum calculation, tamper detection, UTF-8 truncation,
  and the total byte bound including the checksum field.
* `python -m compileall -q framework/agent/models/orchestration.py tests/framework/agent/loop/test_delegate_batch_orchestration.py`,
  `git diff --check`, and strict validation of the active OpenSpec change pass.

This closes the missing integrity field on the generic parent-visible
projection. It does not claim the broader 5.4 qualification: proving every
summary source against gated durable structured evidence and implementing
verified spill-reference resolution remain part of the wider integration gate.
