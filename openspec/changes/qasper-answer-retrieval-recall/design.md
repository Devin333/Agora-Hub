## Context

QASPER answer evaluation currently creates a `CachedFixtureChunkStore` and passes it to `PaperRAGSession`. The production retrieval pipeline already supports dense, sparse, field and rerank stages, but the answer evaluator does not inject a field index or reranker. The result report explicitly says `chunk_store_only; no reranker; no field index`, so the 300-question run cannot distinguish a retrieval ceiling from an answer-generation failure.

The fixture has a frozen dataset/mapping and a base embedding cache. Query vectors may be resolved through the existing `QueryVectorResolver`, while chunk and field vectors must remain offline and identity-bound. The final answer context is bounded by the Harness budget and `AnswerGenerator`; candidate overfetch must not silently raise the number of passages sent to the model.

## Goals / Non-Goals

**Goals:**

- Make the answer-evaluation retrieval profile explicit and observable.
- Reuse the existing `CachedFixtureFieldIndex`, `RetrievalPolicy`, `PaperRAGSession`, and `RerankerPort` contracts.
- Support deterministic lightweight reranking for offline evaluation; a future CrossEncoder profile would be confined to composition and is outside this implementation.
- Fail before a run when the selected field index cannot be built from the supplied cache.
- Record the selected policy, component availability, overfetch and context budget in the run identity.
- Preserve a fixed final context budget and enable a deterministic coverage replay before expensive answer generation.

**Non-Goals:**

- Changing production Research defaults or writing to Qdrant, business memory, active skills, or production indexes.
- Claiming fixture retrieval latency or coverage is production Qdrant quality.
- Replacing the existing QASPER mapping, scoring, sampling, judge, or answer protocol.
- Running a full 300-question live answer evaluation in this code change.

## Decisions

### D1. Components are selected in composition

`QasperAnswerEvaluationService` receives optional `field_index`, `reranker`, and `field_reranker` ports. `interfaces/composition/qasper_evaluation.py` constructs the fixture field index from the same cache and selects a deterministic `LightweightLexicalReranker` for the offline profile. The application layer only depends on ports and does not import infrastructure. A future CrossEncoder profile can be assembled in composition without changing the application service.

Alternative: instantiate infrastructure directly in the application. Rejected because it violates the layer boundary and makes tests require model packages or downloads.

### D2. Use named or explicit policy, never implicit activation

The answer run receives a `RetrievalPolicy` explicitly. The selected profile controls whether reranker ports are injected; the existing policy gates decide whether each query can use them. An empty intent allowlist means all intents are allowed, matching `intent_allowed`; a non-empty list restricts execution to its listed intents. Component availability and actual calls are reported separately. The profile identity includes the policy settings, overfetch multiplier, rerank intent allowlists, and component names.

Alternative: always switch the evaluator to `paper_hybrid_rrf_rag_v1`. Rejected because a policy change must be explicit and comparable; the default answer run remains backward compatible.

### D3. Field cache completeness is a precondition

The evaluator constructs `CachedFixtureFieldIndex` from the base cache and the query resolver. Missing field text keys or dimension/identity mismatches raise the existing offline cache error before the first question. There is no chunk-only fallback after a field profile is selected.

Alternative: skip fields that are not cached. Rejected because it would silently change the experiment and reproduce the known recall blind spot.

### D4. Candidate expansion is separated from context budget

Overfetch and candidate limits are controlled by `RetrievalPolicy` and recorded in retrieval metadata. `PaperRAGSession` retains its safe RAG budget and `AnswerGenerator` retains its eight-passage default. Tests assert that candidate expansion can increase retrieval candidates without increasing accepted context items.

### D5. Coverage replay precedes live answering

The first validation uses the existing deterministic retrieval/evidence mapping path with the same sample, mapping digest, corpus and cache identities. It reports strict/equivalent coverage and candidate provenance for every planned question, including failures and unmappable cases. It does not call the answer model or judge and does not alter gold mappings. A live 300-question rerun is a separate operational step after the replay passes.

The replay itself never embeds a missing query. A runtime miss blocks all coverage publication and records the query text/digest and affected cases/profiles. If a legitimate controlled replan needs an uncached vector, a separate preparation operation may copy the supplementary cache and extend the copy with the same real embedding provider/model/dimension. The original cache and blocked artifacts remain unchanged; the next replay records the new cache content identity and runs every profile against that same snapshot.

## Risks / Trade-offs

- [Risk] The current cache may not contain every field text vector. → The run fails in preflight with the missing key count; no partial score is published.
- [Risk] Lightweight lexical reranking is weaker than a cross-encoder. → The manifest names the component and treats it as a deterministic benchmark profile; it is not presented as production reranking.
- [Risk] More candidates increase retrieval CPU time. → Keep the final context budget fixed and record candidate limits and timings.
- [Risk] A named policy may produce different results from the historical run. → Preserve a baseline profile, record full fingerprints, and compare paired Strict/Equivalent coverage with fixed denominators.

## Migration Plan

1. Add component ports and field-cache preflight to answer evaluation with backward-compatible defaults.
2. Add deterministic component/profile and context-budget tests.
3. Run compile, focused QASPER tests, OpenSpec strict validation, and the repository smoke gate.
4. Run a new offline coverage replay in a separate external result directory; do not overwrite the 300-question answer artifacts.
5. Only after the replay is valid, run a separately identified live answer evaluation if requested. Rollback is the previous composition path with no field/reranker injection.

## Open Questions

- Which policy/profile wins the deterministic replay is intentionally unresolved; it must be decided from the recorded paired coverage rather than hard-coded in this change.
- Whether to use CrossEncoder reranking for a later run remains an operational choice requiring an explicit model/cache and cost budget.
