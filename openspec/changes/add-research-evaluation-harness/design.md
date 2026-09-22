## Context

The repository already contains evidence evaluation, RAG golden-set, and CI promotion-gate capabilities. It also has Harness runtime rules requiring bounded `PLAN -> EXECUTE -> VERIFY` execution, deterministic verification, durable transition records, and explicit budget controls. The missing piece is a single evaluation surface that can exercise the full Research path and report both product quality and workflow integrity.

The evaluation system must run offline for deterministic checks, preserve the real production composition for end-to-end checks, and avoid allowing an LLM judge to make routing, publication, memory, skill, or quality-gate decisions. The first release is an evaluation product and data contract; it does not replace the production Research runtime.

## Goals / Non-Goals

**Goals:**

- Define a versioned, schema-validated evaluation case and run manifest.
- Reuse existing evidence, citation, golden-set, and CI evaluation primitives through library APIs.
- Make deterministic failures reproducible and attributable to a runtime phase.
- Validate bounded Harness behavior, durable transcripts, and side-effect authority.
- Produce machine-readable metrics and reviewer-friendly Markdown summaries.
- Establish a 50-case baseline and support regression comparison by dataset and evaluator version.

**Non-Goals:**

- Training, fine-tuning, or selecting a new model provider.
- Replacing the current Research production runtime or its public interfaces.
- Treating an LLM judge as the sole quality gate.
- Calling live external sources in deterministic Harness failure tests.
- Automatically promoting skills, memories, policies, or reports based on evaluation output.

## Decisions

### 1. Library-first evaluator with a thin CLI

The evaluator will expose structured Python APIs for application and test callers. A CLI will parse options and delegate to the same API. This follows the existing evidence-evaluation contract and avoids coupling business evaluation to argv construction. Alternatives considered: a CLI-only runner would be easy to start but difficult to compose in Harness tests and CI; a separate service would add deployment and persistence complexity before the baseline exists.

### 2. Versioned case manifests instead of embedded test logic

Cases will be stored as versioned JSON or YAML records with stable IDs and explicit expectations. The runner records dataset version, evaluator version, repository revision, model configuration, and run ID in the manifest. Alternatives considered: Python-only fixtures would offer flexibility but make review, diffing, and reproducibility harder.

### 3. Deterministic gate before semantic review

Schema, traceability, citation, budget, transition, and architecture checks run first and can fail a case without a judge call. Semantic review is an optional second layer with separate scores and calibration metadata. Alternatives considered: a single LLM judge is less work initially but cannot reliably enforce budgets, transitions, or artifact existence and would make failures expensive to reproduce.

### 4. Fault injection through explicit test adapters

Harness scenarios use fake source, worker, tool, verifier, and storage adapters that implement the same application contracts as production components. They inject invalid plans, timeouts, retries, budget exhaustion, conflicts, and write failures deterministically. Alternatives considered: mocking internal functions would overfit tests to implementation details and would not prove the production boundary contracts.

### 5. Evaluation artifacts are append-only per run

Each run writes a manifest, case inputs, outputs, metrics, failures, and summary under `evaluations/runs/<run_id>/`. Phase transitions and evaluator decisions include stable case and artifact references. A run is never overwritten; a repeated run receives a new ID. Alternatives considered: a single aggregate file would be smaller but would prevent replay, audit, and reliable baseline comparison.

### 6. Three execution tiers

The fast tier runs deterministic offline cases and architecture checks on pull requests. The scheduled tier runs the broader corpus and records latency/cost distributions. The release tier runs all critical cases and requires human review for critical failures. This keeps PR feedback bounded while retaining coverage for live behavior. Alternatives considered: running the full live corpus on every PR would be slow, flaky, and expensive.

### 7. Reuse before introducing new abstractions

The implementation will call existing structured evidence-evaluation and RAG evaluation APIs where they already expose the needed contract. New evaluator modules will own only cross-stage orchestration, case loading, metric aggregation, and Harness-specific assertions. No compatibility layer will be added for legacy Research paths that the active architecture rules forbid.

## Risks / Trade-offs

- **[Risk]** External source changes make live cases unstable. **Mitigation:** pin captured evidence for deterministic cases, record source snapshots and timestamps, and separate live scheduled runs from PR gates.
- **[Risk]** Metrics hide severe errors behind a high aggregate score. **Mitigation:** enforce critical-failure vetoes for fabricated citations, unsupported critical claims, gate bypass, missing transcripts, and budget violations.
- **[Risk]** The Golden Set becomes stale. **Mitigation:** version datasets, record ownership and review dates, and require an explicit dataset version in every run.
- **[Risk]** Semantic judge scores drift or disagree with reviewers. **Mitigation:** keep judge output advisory, store rubric/version metadata, and manually calibrate a fixed sample.
- **[Risk]** Evaluation code accidentally changes production routing. **Mitigation:** keep evaluation adapters behind application-level interfaces and add architecture tests proving no forbidden legacy dependency or direct executor/store access.
- **[Risk]** Artifact volume grows without retention rules. **Mitigation:** make retention configurable and preserve manifests plus failure evidence for every baseline and release run.

## Migration Plan

1. Add schemas, evaluator APIs, fixtures, and report serialization without changing production runtime behavior.
2. Add deterministic Harness scenarios and run them locally with the existing developer tooling.
3. Add the initial 50-case Golden Set and generate the first baseline report.
4. Enable the fast tier in pull-request CI after the baseline is reviewed.
5. Enable scheduled and release tiers after external-source stability and human calibration are established.

Rollback consists of disabling the new evaluation command/CI job and retaining already generated run artifacts; no production data migration or runtime rollback is required.

## Open Questions

- Which repository-owned location should become the long-term source-of-truth directory for the 50-case Golden Set after the first baseline review?
- Which existing report/evidence artifact store should receive promoted evaluation summaries, if any, after the initial file-based run artifacts are accepted?
- What cost and latency thresholds should be made blocking after one week of scheduled baseline data?
