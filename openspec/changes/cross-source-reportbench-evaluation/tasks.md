## 1. Domain contract

- [x] 1.1 Add ReportBench-compatible task, source, citation, execution metadata, result, and validation issue models under `backend/research/benchmark/`.
- [x] 1.2 Implement deterministic validation for task identity, one-result-per-case, report text, source identity, HTTPS URLs, source kinds, and sanitized failed-run records.
- [x] 1.3 Implement JSONL serialization/deserialization and conflict-safe byte-identical export helpers.

## 2. Runner and application boundary

- [x] 2.1 Add an injectable cross-source research runner protocol that returns the domain result without importing UI, executor, LLM, scraper, or storage implementations.
- [x] 2.2 Add an application service for loading local benchmark tasks, generating bounded diagnostic run plans, validating results, and exporting records.
- [x] 2.3 Ensure validation/export/run-plan report zero network and paid calls and never make a production quality or publication decision.

## 3. CLI and documentation

- [x] 3.1 Add `reportbench` CLI commands for `validate`, `export`, and `run-plan`, including explicit input/output/manifest arguments and stable JSON output.
- [x] 3.2 Register the command in `scripts.dev` without changing existing RAG commands or their arguments.
- [x] 3.3 Document the ReportBench adapter, 3-case canary workflow, output format, non-qualified status, and later approval boundary.

## 4. Tests and verification

- [x] 4.1 Add offline fixtures for valid multi-source, duplicate source, unsafe URL, missing source, failed run, and conflicting destination cases.
- [x] 4.2 Add unit/application/CLI tests covering all scenarios in both new specs, including no-network/no-paid-call assertions.
- [ ] 4.3 Run targeted tests, compile, `openspec validate cross-source-reportbench-evaluation --strict`, and `python -m scripts.dev smoke`.
- [x] 4.4 Review the final diff, stage only this change plus its implementation files, and commit on `codex/reportbench-cross-source-eval`.
