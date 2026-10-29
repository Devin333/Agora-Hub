## Why

Agora Hub has a real-paper retrieval benchmark and a BGE cross-encoder, but no measured evidence that Jev improves reranking quality, latency, or cost. A fixed-input comparison is needed before considering a production integration.

## What Changes

- Add an opt-in experiment command that freezes a question/candidate manifest from the existing reviewed golden set and real corpus.
- Compare the existing lexical and BGE rankers with a real TypeSafe Jev API call on identical candidate text, without exposing gold labels to providers.
- Persist per-question results, failures, timings, token usage, model identity, hashes, and an aggregate report; missing credentials remain explicitly unmeasured.
- Keep all experiment behavior outside production routing and quality gates.

## Capabilities

### New Capabilities

- `jev-rerank-comparison`: Reproducible, bounded reranking experiments with honest partial-result reporting.

### Modified Capabilities

None.

## Impact

- New experiment code under `scripts/`, focused tests, and experiment documentation.
- Reuse checked-in evaluation data and existing reranker implementations; no new mandatory dependency or production configuration.
- Real Jev inference requires `TYPESAFE_API_KEY`; model weights for the BGE baseline are already available locally.
