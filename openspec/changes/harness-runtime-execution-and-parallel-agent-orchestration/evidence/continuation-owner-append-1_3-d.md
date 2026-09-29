# Continuation append owner: task 1.3 evidence slice

Change: `harness-runtime-execution-and-parallel-agent-orchestration`.
Scope: align the online TaskPlan append owner with offline continuation replay for
recorded group, parent/submission, observation, and terminal state. This is a
contract slice; task 1.3 and parent continuation tasks 5.2/5.3 stay unchecked.

## Defect and owner boundary

Before this change, the memory/SQLite append CAS validated a continuation's
checksum, identity, version, and immutable PENDING-to-terminal update, but did
not bind a newly written continuation to the already recorded TaskPlan prefix.
A checksum-valid `DELIVERED/SUCCEEDED` claim for an ADMITTED group with no
recorded terminal observation could be committed online and then rejected by
offline replay with `parent_continuation_terminal_mismatch`.

`validate_parent_continuation_owner_append` now checks each newly appended
continuation against its exact preceding canonical event prefix. The group must
be recorded in the same run/stage; a prior continuation and a recorded
`CandidateSubmission` pin the parent turn and submission; a supplied submission
cannot invent an unrecorded authority. Join observations are checksum-checked
from `TASK_GROUP_JOIN_WAITING`/`TASK_GROUP_JOINED`; the existing
`validate_continuation_projection` enforces the matching observation and
terminal group state. `validate_parallel_admission_append` calls this owner
validator before memory/SQLite event, projection, or artifact mutations.

## Executed checks

- Memory and SQLite reject checksum-consistent forged parent/submission claims,
  including a swap to another genuinely recorded parent/submission in the same
  run/stage, without changing history, projection, or results.
- Memory and SQLite reject a fabricated terminal continuation before group
  termination and canonical observation; the SQLite test also checks unchanged
  stream high watermark and immutable artifact content and a replayable prefix.
- SQLite accepts `PENDING -> JOIN_WAITING -> FAILED -> DELIVERED`, handles an
  identical terminal retry without a new event, and reopens with the same
  projection/continuation and offline replay outcome.
- Focused continuation/parallel suites passed (79 tests) and AgentLoop-focused
  suites passed (51 tests); `python -m scripts.dev compile`, strict OpenSpec
  validation, and `git diff --check` passed.
- Required `python -m scripts.dev smoke` passed: 3781 passed, 23 deselected,
  31 warnings in its test phase; its AgentLoop smoke succeeded and
  `sources validate` returned `is_valid=true`, zero errors/warnings.

This slice does not claim full 1.3 coverage for all identity/capability/policy/
reference/schema/transition families, complete delivery and redelivery in 5.3,
full producer routing in 3.1, or release qualification. The owner check covers
TaskPlan store mutations only; it makes no cross-owner atomicity claim.
