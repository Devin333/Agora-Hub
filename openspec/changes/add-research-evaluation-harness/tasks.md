## 1. Contracts and Dataset

- [ ] 1.1 Define typed evaluation case, manifest, run, metric, failure, and semantic review contracts with explicit schema versions.
- [ ] 1.2 Add the initial 50-case Golden Set with the required category labels, source constraints, expected requirements, forbidden claims, and risk levels.
- [ ] 1.3 Add dataset validation for required fields, duplicate IDs, supported versions, category counts, and deterministic fixture references.

## 2. Deterministic Evaluation Core

- [ ] 2.1 Implement a library-first evaluation runner that accepts structured options and exposes a thin CLI adapter.
- [ ] 2.2 Implement requirement coverage, factual support, citation validity/correctness/coverage, source quality, traceability, and critical-failure evaluators.
- [ ] 2.3 Implement metric aggregation, configured thresholds, regression deltas, and CI-compatible exit codes.
- [ ] 2.4 Add architecture-boundary checks for forbidden Research imports, interface-to-service ownership, and worker side-effect authority.

## 3. Harness Scenario Evaluation

- [ ] 3.1 Add fake source, worker, tool, verifier, and artifact-store adapters behind the production application contracts.
- [ ] 3.2 Add deterministic scenarios for invalid plans, invalid worker output, tool timeout, partial collection failure, conflicting sources, and storage failure.
- [ ] 3.3 Add retry, replan, max-turn, and budget-exhaustion scenarios that assert terminal status and no post-halt publication.
- [ ] 3.4 Add transcript assertions for every legal and illegal phase transition and verify replayable references.

## 4. Artifacts and Reporting

- [ ] 4.1 Write append-only run artifacts under `evaluations/runs/<run_id>/` with the required JSONL, JSON, and Markdown files.
- [ ] 4.2 Include repository revision, dataset/evaluator versions, model configuration, timing, call counts, cost signals, and failure references in the run manifest.
- [ ] 4.3 Add semantic review storage with rubric/version metadata and deterministic-result separation.
- [ ] 4.4 Add baseline comparison output for metric deltas, newly failing cases, and configured regression thresholds.

## 5. Tooling and CI

- [ ] 5.1 Expose the fast offline evaluation tier through the repository developer command surface.
- [ ] 5.2 Add pull-request CI execution for deterministic cases and architecture checks without external services.
- [ ] 5.3 Add scheduled corpus evaluation and release evaluation entry points with human-review artifacts for critical failures.
- [ ] 5.4 Document dataset ownership, case authoring, evaluator versioning, artifact retention, and triage workflow.

## 6. Verification and Rollout

- [ ] 6.1 Add unit and scenario tests for every evaluator requirement and critical-failure veto.
- [ ] 6.2 Run the initial baseline and review metric calibration against a fixed human sample.
- [ ] 6.3 Run `python -m scripts.dev compile`, `python -m scripts.dev test`, and `python -m scripts.dev smoke` and fix all regressions.
- [ ] 6.4 Run `openspec validate add-research-evaluation-harness --strict` and record the baseline report before enabling CI promotion.
