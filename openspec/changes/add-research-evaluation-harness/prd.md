# Research Evaluation Harness PRD

## 1. Document Information

- **Product:** Agora Hub Research Evaluation Harness
- **Change:** `add-research-evaluation-harness`
- **Status:** Proposed
- **Owner:** Research and Harness runtime maintainers
- **Primary users:** Research engineers, Harness engineers, release reviewers, and CI maintainers
- **Related OpenSpec capability:** `research-evaluation-harness`

## 2. Problem Statement

The project has evaluation pieces for evidence and RAG, but it does not yet have one repeatable evaluation product for the complete Research runtime. A change can improve the final prose while silently regressing source collection, evidence grounding, citation correctness, bounded Harness execution, retry budgets, durable transcripts, or stored artifacts.

The team needs a reproducible baseline that answers four questions for every run:

1. Did the Research result answer the requested question and cover the required facts?
2. Can every important claim be traced to valid evidence and a source?
3. Did the Harness follow the bounded `PLAN -> EXECUTE -> VERIFY` workflow and authority boundaries?
4. Can another engineer replay, inspect, compare, and diagnose the run?

## 3. Product Goal

Provide a versioned, deterministic-first evaluation workflow for the existing Research path:

```text
source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage
```

The workflow must produce a trustworthy first baseline and make later code, prompt, model, and configuration changes comparable without changing the production Research path.

## 4. Users and User Stories

### Research Engineer

- As a Research engineer, I want to run a selected set of cases locally so that I can reproduce a reported regression before changing production code.
- As a Research engineer, I want claim, evidence, and source references in a failure report so that I can repair grounding issues instead of guessing from the final prose.

### Harness Engineer

- As a Harness engineer, I want deterministic retry, replan, timeout, and budget scenarios so that workflow safety does not depend on an external model or source service.
- As a Harness engineer, I want every phase transition and terminal decision persisted so that I can inspect whether the runtime stopped at the correct boundary.

### Release Reviewer

- As a release reviewer, I want a baseline comparison showing metric deltas and newly failing cases so that I can decide whether a change is safe to promote.
- As a release reviewer, I want critical failures to veto the aggregate score so that fabricated citations or gate bypasses cannot be hidden by average quality.

### CI Maintainer

- As a CI maintainer, I want a fast offline command with a stable exit code so that pull requests can enforce deterministic regressions without live service flakiness.
- As a CI maintainer, I want scheduled and release tiers separated from the pull-request tier so that broad live evaluation remains observable without slowing every change.

## 5. Scope

### In Scope

- Versioned evaluation case and run contracts.
- A first Golden Set of 50 cases.
- Deterministic evaluation of content, evidence, citations, traceability, Harness transitions, budgets, transcripts, artifacts, and architecture boundaries.
- Optional semantic review stored separately from deterministic results.
- Offline fake adapters for controlled Harness failure scenarios.
- JSON and Markdown run artifacts.
- Local runner, baseline comparison, and CI-compatible exit codes.
- Fast pull-request, scheduled, and release evaluation tiers.
- Documentation for authoring cases, reviewing failures, and maintaining versions.

### Out of Scope

- Model training, fine-tuning, or provider selection.
- Replacing or restructuring the production Research runtime.
- Making an LLM judge the sole quality gate.
- Automatically promoting skills, memories, policies, or reports from evaluation output.
- Requiring live external services for deterministic Harness tests.
- Building a new evaluation web UI in the first release.

## 6. Product Workflow

1. An operator selects a dataset version, optional case IDs, a run ID, and an evaluation tier.
2. The runner validates the case manifest before invoking the Research path.
3. The selected runtime executes with real application services or controlled fake adapters, depending on the tier.
4. The deterministic evaluator checks schema, requirements, source constraints, citations, evidence lineage, phase transitions, budgets, transcripts, side-effect authority, and artifact completeness.
5. An optional semantic reviewer scores relevance, completeness, source quality, and clarity. Its result is stored separately and cannot override deterministic failures.
6. The runner aggregates per-case and global metrics, applies configured thresholds and critical-failure vetoes, and compares with an optional baseline.
7. The runner writes append-only artifacts and returns a CI-compatible exit code.

## 7. Functional Requirements

### 7.1 Case and Dataset Management

- Every case has a stable `id` and a schema version.
- Every case includes `query`, `as_of_date`, `expected_requirements`, `required_source_types`, `forbidden_claims`, `minimum_sources`, and `risk_level`.
- Cases may define expected behavior, critical claims, metric thresholds, source snapshots, and deterministic adapter fixtures.
- The first dataset contains 20 normal Research cases, 10 evidence/citation cases, 10 Harness failure cases, and 10 boundary/security cases.
- Duplicate IDs, unsupported versions, missing required fields, and invalid category counts fail before execution.

### 7.2 Deterministic Content and Evidence Evaluation

- Validate the output schema before semantic scoring.
- Calculate requirement coverage against the case's expected requirements.
- Identify factual claims and check their evidence and citation references where the output contract exposes them.
- Check citation validity, citation correctness, citation coverage, source constraints, and source quality markers.
- Verify report-to-evidence-to-source traceability and artifact references.
- Fail a case for fabricated sources, unsupported critical claims, missing evidence for required claims, or broken traceability regardless of aggregate score.

### 7.3 Harness and Architecture Evaluation

- Validate legal `PLAN -> EXECUTE -> VERIFY` transitions.
- Verify that VERIFY is performed by deterministic gates.
- Verify that failed verification follows allowed retry/replan behavior.
- Enforce `max_replans`, `max_turns`, and retry budgets.
- Verify that a halted run cannot publish output or continue side effects.
- Verify every phase transition has a durable transcript/event record.
- Detect forbidden Research dependencies on legacy `backend/boards/paper_radar`, `interfaces`, or `infrastructure` during the rebuild.
- Detect worker attempts to decide routing, quality pass/fail, publication, memory writes, tool authorization, or skill promotion.

### 7.4 Controlled Failure Evaluation

Provide fake adapters that implement application-level contracts for:

- Source collection timeout and partial failure.
- Invalid plan.
- Invalid worker output.
- Tool timeout.
- Conflicting sources.
- VERIFY retry then pass.
- VERIFY retry/replan budget exhaustion.
- Artifact storage write failure.
- Evidence prompt injection or side-effect request.

Each scenario must assert the terminal status, failure category, transcript, artifact references, and absence of unauthorized side effects.

### 7.5 Semantic Review

Support a human or LLM review adapter for:

- Relevance to the user question.
- Completeness and limitations.
- Source authority and independence.
- Clarity and report structure.

Semantic review records must include reviewer type, rubric version, timestamp, and per-dimension scores. Semantic scores are advisory and cannot turn a deterministic failure into a pass.

### 7.6 Run Artifacts and Reports

Each run writes a unique directory:

```text
evaluations/runs/<run_id>/
  manifest.json
  cases.jsonl
  outputs.jsonl
  metrics.json
  failures.jsonl
  summary.md
```

The manifest records repository revision, dataset version, evaluator version, tier, configuration, model metadata when available, and run timestamps. Case records retain inputs, source/evidence references, plans, transitions, worker candidates, verification results, final reports, latency, call counts, and cost signals.

### 7.7 Runner and Execution Tiers

The runner supports:

- Dataset version selection.
- Case ID selection.
- Explicit run IDs.
- JSON and Markdown output.
- Baseline comparison.
- CI-compatible exit codes.

The tiers are:

- **Fast PR tier:** deterministic offline cases and architecture checks; no external services.
- **Scheduled tier:** broader corpus, live or captured source evaluation, latency and cost distributions.
- **Release tier:** complete critical-case set, baseline comparison, and human review of critical failures.

## 8. Metrics and Initial Targets

| Metric | Definition | Initial target |
| --- | --- | --- |
| Requirement Coverage | Required facts satisfied / required facts | >= 85% |
| Factual Precision | Supported factual claims / factual claims | >= 95% |
| Unsupported Claim Rate | Unsupported claims / total claims | <= 3% |
| Citation Validity | Resolvable citations / citations | >= 98% |
| Citation Correctness | Citations supporting their claims / citations | >= 95% |
| Citation Coverage | Cited claims / claims requiring citations | >= 95% |
| Traceability | Reports with valid report-evidence-source lineage | 100% |
| Invalid Transition Rate | Illegal transitions / transitions | 0 |
| Budget Violation Rate | Runs exceeding configured budgets | 0 |
| Transcript Completeness | Transitions with durable records | 100% |
| Gate Bypass Count | Runs bypassing deterministic verification | 0 |

Latency, LLM/tool call count, token count, source count, and estimated cost are baseline measurements in the first release. Blocking performance thresholds are set after one week of scheduled data.

## 9. Critical Failure Policy

The case and run must fail immediately for any of the following:

- Critical factual error.
- Fabricated source or citation.
- Critical claim without supporting evidence.
- Broken report/evidence/source traceability.
- Quality gate bypass.
- Continued execution after a hard budget or terminal halt.
- Missing durable phase transition.
- LLM or worker granted publication, memory-write, skill-promotion, routing, or quality-gate authority.
- Forbidden architecture dependency introduced by the evaluation path.

Critical failures are listed individually in `failures.jsonl` and the Markdown report even when the aggregate metrics pass.

## 10. Acceptance Criteria

The first release is accepted when:

1. The 50-case dataset loads with a versioned manifest and correct category counts.
2. The same deterministic case produces the same evaluator result on repeated runs.
3. Every failed case identifies its phase, category, assertion, and artifact references.
4. Every Harness phase transition is durable and inspectable.
5. The evaluator detects citation failures, traceability failures, gate bypasses, budget violations, and unauthorized side effects.
6. Every run writes the required JSON and Markdown artifacts.
7. Baseline comparison reports metric deltas and newly failing cases.
8. The fast tier runs without external services and returns a stable non-zero code on threshold failure.
9. The evaluation implementation does not change the production Research path.
10. `openspec validate add-research-evaluation-harness --strict` passes.
11. Project compile, tests, and smoke checks pass after implementation.

## 11. Rollout Plan

### Phase 1: Evaluation Core

Implement schemas, manifest loading, deterministic evaluator APIs, metric aggregation, and report serialization.

### Phase 2: Harness Scenarios

Add fake adapters, failure injection, budget/transition assertions, and transcript checks.

### Phase 3: Golden Set and Baseline

Author and review the 50 cases, run the first baseline, calibrate semantic review against a fixed human sample, and record initial latency/cost distributions.

### Phase 4: CI and Release Workflow

Enable the fast PR tier, schedule broad evaluation, define release review artifacts, and establish dataset/evaluator ownership.

## 12. Risks and Mitigations

- **Stale cases:** version datasets, record ownership and review dates, and require a dataset version for every run.
- **Live source instability:** use captured evidence for deterministic cases and isolate live runs to scheduled/release tiers.
- **Score masking:** use critical-failure vetoes instead of relying only on aggregate scores.
- **Judge drift:** keep semantic review advisory and calibrate it against human samples.
- **Artifact growth:** configure retention while preserving baseline and critical-failure evidence.
- **Production coupling:** use application-level contracts and architecture checks to keep evaluation code outside forbidden legacy boundaries.

## 13. Open Decisions

- Confirm the repository-owned long-term location of the Golden Set after the first baseline review.
- Confirm whether evaluation summaries should later be indexed in the existing artifact store.
- Set blocking latency and cost thresholds after one week of scheduled measurements.
