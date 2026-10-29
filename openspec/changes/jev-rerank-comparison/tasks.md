## 1. Reproducible experiment tooling

- [x] 1.1 Prepare and validate a fixed pilot manifest from the reviewed golden set and real corpus snapshot.
- [x] 1.2 Implement bounded lexical, BGE, and Jev execution with per-case observations and strict provider validation.
- [x] 1.3 Implement matching-input summary, quality/latency metrics, usage accounting, and honest missing-arm reporting.
- [x] 1.4 Add focused tests for fairness, provider failures, metric correctness, and manifest mismatch handling.

## 2. Execution and evidence

- [x] 2.1 Run local baseline arms and save manifest, settings, and measurements.
- [ ] 2.2 Run the Jev arm with a real configured key and produce the paired comparison.
- [x] 2.3 Document observed outcomes, limitations, and exact reproduction commands.

## 3. Validation and delivery

- [ ] 3.1 Pass focused tests, required smoke, and strict OpenSpec validation.
- [ ] 3.2 Commit the experiment implementation and available evidence on the isolated branch.

Live execution note (2026-09-20): task 2.2 is pending because `TYPESAFE_API_KEY` is not configured. The Jev report is explicitly `blocked`, with zero completed cases; it is not evidence about Jev quality or latency. Local baseline and summary artifacts are available under `outputs/jev-rerank-pilot-20260920/`.
