## Why

The project has evaluation building blocks for evidence and RAG, but it does not yet provide one repeatable evaluation contract for the complete Research runtime. As a result, a code or model change can improve answer text while regressing source traceability, Harness control, retry budgets, or durable artifacts without a single baseline exposing the regression.

This change establishes a versioned evaluation harness for the existing runtime path (`source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage`) so the team can produce a first trustworthy baseline before optimizing models or production behavior.

## What Changes

- Add a versioned Research evaluation case contract covering queries, expected requirements, source constraints, forbidden claims, risk, and thresholds.
- Add a deterministic evaluator for schema validity, requirement coverage, citation validity and correctness, evidence-to-report traceability, phase transitions, budget enforcement, and architecture guardrails.
- Add a semantic review adapter whose results are stored separately from deterministic gate results and can be sampled for human calibration.
- Add controlled fake source, worker, gate, and storage adapters for repeatable Harness failure scenarios without external services.
- Add a 50-case first Golden Set: normal Research, evidence/citation, Harness failure, and boundary/security cases.
- Add a local evaluation runner that supports dataset and case selection, run IDs, baseline comparison, JSON/Markdown reports, and CI-compatible exit codes.
- Persist complete evaluation artifacts, including inputs, sources, evidence, plans, phase transitions, worker candidates, verification results, final reports, metrics, failures, and runtime cost signals.
- Add three execution tiers: fast pull-request checks, scheduled corpus evaluation, and release evaluation with human review of critical failures.
- Preserve the existing Research production path and reuse existing evidence and RAG evaluation primitives where their contracts already satisfy this change.

## Capabilities

### New Capabilities

- `research-evaluation-harness`: Provides a versioned, reproducible evaluation contract for Research output quality, evidence traceability, bounded Harness execution, and evaluation artifacts.

### Modified Capabilities

- None.

## Impact

- New evaluation domain code under the repository evaluation package and corresponding fixtures/tests.
- New versioned evaluation data under the repository evaluation corpus area.
- New JSON/Markdown run artifacts under `evaluations/runs/<run_id>/`.
- A local developer command and CI wiring for the fast evaluation tier; existing compile, test, and smoke commands remain unchanged.
- No mandatory production dependency, external service, model provider, or public Research API change.
- Existing uncommitted files outside this change are unaffected.
