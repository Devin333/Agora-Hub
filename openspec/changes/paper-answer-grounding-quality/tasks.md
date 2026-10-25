## 1. Freeze the measurement contract

- [ ] 1.1 Define the versioned quality manifest for dataset, sample order, paper-family split, corpus/mapping, cache identities, code revision, prompt/protocol, retrieval policy, model/provider, candidate schema, gate/verifier/judge versions, metric formulas, thresholds, budgets, and allowed differences.
- [ ] 1.2 Add immutable parent-artifact and per-attempt lineage validation for report, answers, sample, recovery, rejudge, and candidate-comparison outputs; reject missing identities instead of filling current defaults.
- [ ] 1.3 Freeze metric ledgers for planned cases, successful/failed/ungraded outcomes, correct answers, correct refusals, false refusals, key-fact completeness, citation micro/macro, invalid/unknown/missing judgments, and generation/judge/provider errors; preserve the historical 174/213 and 82.98% bridge labels.
- [ ] 1.4 Complete the pending human judge calibration workflow using blinded risk-stratified samples, adjudication, and paper-family separation; persist calibration and conflict artifacts with a versioned verdict identity.
- [ ] 1.5 Add tests proving old artifacts remain byte-stable, missing identities block comparison, unknown/NA/error cases cannot inflate a denominator, and calibration/holdout leakage is rejected.

## 2. Preserve evidence and actual model-visible context

- [ ] 2.1 Extend the framework evidence contract and adapter to carry lossless body/reference data separately from summary, including content digest, source version, locator, and verifiable artifact references; keep Paper-specific types in Research.
- [ ] 2.2 Update Paper projection and answer-worker context reconstruction to stop using the 1200-character summary as an implicit body and to fail closed when the complete body reference cannot be resolved.
- [ ] 2.3 Implement deterministic passage selection and content/span deduplication that preserves source aliases, offsets, overlap provenance, qualifiers, and original evidence objects.
- [ ] 2.4 Make the final serialized provider request enforce item, character, token, and reserved-output budgets using the actual message representation; record trim/reject reasons rather than capping an estimate only.
- [ ] 2.5 Persist and expose a `VisibleContextSnapshot` for every generation attempt, including exact rendered passages, numbering, source/content hashes, offsets, render version, budget, truncation, request digest, and provider-facing message identity.
- [ ] 2.6 Add unit, integration, and deterministic replay tests for long evidence, missing body artifacts, duplicate passages, overlapping spans, budget overflow, and equality between snapshot text and citation/evaluation input.

## 3. Produce atomic claims and exact citation bindings

- [ ] 3.1 Define the versioned internal candidate contract for stable `claim_id`, answer span, atomic factual text, qualifiers/polarity/numeric roles, evidence binding, exact quote, source offsets, and visible-context identity while keeping the external answer payload compatible.
- [ ] 3.2 Update the Paper answer worker and prompt/parser to emit independently supportable claims and explicit bindings; remove whole-answer claim plus all-evidence fallback under strict policy.
- [ ] 3.3 Implement deterministic claim renderer that emits public answer text and adjacent citation markers only from structurally validated claims, preserving material qualifiers and answer coverage.
- [ ] 3.4 Extend the answer gate to validate claim coverage, quote/offset equality, visible snapshot membership, source version, answer spans, typed values, polarity, and citation ownership; return actionable failure reasons.
- [ ] 3.5 Add regression fixtures for the four known citation failure families and for boolean polarity, numerical qualifiers, multi-source composite statements, missing citations, invalid spans, and unsupported whole-answer fallback.

## 4. Calibrate semantic observations and bounded repair

- [ ] 4.1 Implement a separate verifier observation contract for supported, contradicted, insufficient, and unknown results based only on the actual visible snapshot; exclude gold answers/evidence and generator self-reported support.
- [ ] 4.2 Connect verifier observations to the existing Harness `PLAN -> EXECUTE -> VERIFY` and supplemental-round policy; keep routing, pass/fail, retry, replan, halt, and cost authority in Harness.
- [ ] 4.3 Add bounded claim repair and evidence-gap supplemental retrieval actions with shared `max_attempts`, `max_replans`, and cost budgets; prohibit gold-driven runtime queries and unbounded loops.
- [ ] 4.4 Persist candidate, snapshot, verifier observation, gate result, phase transition, repair action, remaining budget, and final decision in the durable transcript; support deterministic replay without provider calls.
- [ ] 4.5 Add tests for supported-but-structurally-invalid, unsupported, contradicted, unknown, budget-exhausted, provider-failure, and repeated-invalid-candidate paths.

## 5. Diagnose reliability and improve retrieval

- [ ] 5.1 Add a bounded, frozen 502 diagnostic runner for the 45 repeated failure cases with randomized order, matched successful controls, request/config fingerprints, safe length statistics, provider status/error summaries, and complete attempt/cost accounting.
- [ ] 5.2 Keep failure diagnosis, judge-only recovery, and new prompt/context/model comparisons as separate identities and artifacts; add tests preventing successful-attempt cherry-picking or recovery identity drift.
- [ ] 5.3 Reuse the existing QASPER retrieval profiles, field cache, lightweight reranker, and retrieval-only replay after evidence visibility is fixed; fail preflight on missing cache/query vectors and preserve the final context budget.
- [ ] 5.4 Add single-factor slice reports that distinguish not-recalled, candidate-not-selected, budget-not-visible, and visible-but-unused evidence for false refusals and wrong answers.
- [ ] 5.5 Add a separately identified real-Qdrant transfer check that records index/filter/candidate provenance and actual-visible snapshots; keep fixture results scoped to fixture until transfer is verified.

## 6. Run staged evaluation and quality qualification

- [ ] 6.1 Run offline contract/replay and focused regression checks without live model calls; block progression when evidence identity, budget, gate, or metric ledgers are incomplete.
- [ ] 6.2 Run preregistered small paired single-factor experiments, retaining every attempt and cost; compare projection, claim/gate, retrieval, and prompt factors separately.
- [ ] 6.3 Run the untouched paper-family holdout with frozen configuration and blinded scoring; once observed, retire it from future tuning and prepare a fresh holdout if changes are made.
- [ ] 6.4 Run the final 300-question development/regression suite only after the holdout gate, and report the historical and new metric protocols side by side without relabeling the 300 as holdout.
- [ ] 6.5 Compute paper-clustered paired uncertainty, disclose effective papers, failures, unknowns, and costs, and classify each scope as `passed`, `failed`, `inconclusive`, or `unverified`.
- [ ] 6.6 Require joint qualification: visible citation support micro at least 98%, preregistered nonzero improvement in overall correctness and correct answer yield, reduced false refusal, non-regressed key-fact completeness, complete citation judgments, and cost/error ceilings.

## 7. Validate and deliver

- [ ] 7.1 Add focused tests for framework adapters, context snapshots/budgets, claims/rendering/gates, verifier policy, transcript replay, QASPER ledgers, and failure diagnostics.
- [ ] 7.2 Run `openspec validate paper-answer-grounding-quality --strict`, `python -m scripts.dev compile`, and the required `python -m scripts.dev smoke` after code implementation; fix root causes before delivery.
- [ ] 7.3 Review the final manifest, reports, lineage, security redaction, rollback policy, and OpenSpec task completion; commit the completed implementation and documentation as one reviewable change.
