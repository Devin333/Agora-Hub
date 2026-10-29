## Context

The checked-in benchmark contains 79 reviewed questions (67 answerable and 12 abstention cases) and a content-addressed snapshot of 35 real chunks from 20 papers. The full local corpus is absent. BGE weights are cached, but this machine has CPU-only PyTorch. Jev credentials are not initially configured.

## Goals / Non-Goals

**Goals:** Produce a reproducible pilot comparing lexical, BGE, and Jev reranking on identical inputs, including incomplete-run reporting.

**Non-Goals:** Production integration, gate changes, answer generation, abstention calibration, full-corpus retrieval claims, or automatic provider promotion.

## Decisions

1. Freeze a seed-selected sample of 24 answerable questions from the existing reviewed set. Exclude and count abstention cases. Rank all snapshot chunks using the existing lexical scorer to select 12 candidates per question; do not inject known gold chunks. This tests reranking on a small cross-paper pool, not paper-scoped production recall. Preserve missed gold and report candidate coverage as an upper bound.
2. Use a versioned manifest with `schema`, `metadata`, and `cases`; each case has `id`, `paper_id`, `query`, `gold_ids`, and ordered `candidates` with `id`, `text`, `paper_id`, `source_ref`. Gold IDs are evaluation-only and never sent to model providers. Snapshot validation and source-file hashes bind the inputs. Candidate text is identical and unmodified across arms; BGE's native 512-token limit is recorded as a comparison limitation.
3. Separate dataset preparation from execution. Each arm writes its own JSON report so baseline runs can finish without Jev credentials; a summarize command combines only matching dataset fingerprints. Save per-query rankings, raw validated Jev answers, failures, request usage, resolved model, and elapsed time. Re-runs use new output paths.
4. Jev uses the documented HTTPS endpoint with pinned `jev-1.13.0`, a four-level relevance Score, and at most 8 passages per request by default. The shared state carries the query and candidate text; each question explicitly names its passage ID. Preserve independent scores rather than using a mutually exclusive Choice across passages. Enforce bounded calls, timeout, and no implicit retries; record partial failures and never substitute baseline scores under the Jev label.
5. Reuse the existing BGE and lexical implementations. Measure model initialization and a single first-case warmup separately from measured query latency. Count Jev warmup usage in total spend. Use stable ties by candidate order. Report Hit@1/3/5, MRR, nDCG@5, candidate gold coverage, completion rate, and p50/p95 query latency. Paired quality comparisons use the same successfully evaluated cases and also disclose total coverage/failures; a missing arm cannot produce a winner.
6. Report measured token usage and explicitly priced estimated API cost only when a price is supplied. Local hardware cost is unknown, not zero. Pin prompt/model/settings in the report. Keep credentials out of requests saved to disk, errors, logs, and commits; load keys from environment or an explicit local env file.

## Risks / Trade-offs

- Small curated corpus and incomplete relevance labels → label the result a pilot; gold-only binary metrics can penalize valid alternative evidence and cannot establish production quality.
- CPU BGE versus hosted Jev → report device, cold start, and network-inclusive Jev latency; do not extrapolate to GPU deployment.
- Jev key missing or service failure → persist baseline evidence and a clear unmeasured/failed status, leaving live comparison pending.
- Score and confidence can be wrong → no automatic quality pass, routing, publication, or rollout follows this experiment.

## Migration Plan

Opt-in scripts only. Run focused tests, required smoke, and strict OpenSpec validation before committing. No production migration or rollback is needed.
