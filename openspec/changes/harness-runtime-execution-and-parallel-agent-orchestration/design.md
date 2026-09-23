## Context

The two predecessor changes already established most local contracts: execution profiles and provider admission, a Harness-owned child supervisor, canonical runtime events, durable group/wave schemas, capacity reservations, result authority, and Research graph composition. Their remaining work is coupled. A parallel child cannot be safely admitted without an execution capability and lifecycle receipt, and a sandboxed child path is not production-qualified until its restart, cancellation, result verification, and rollout behavior are exercised through the same coordinator.

The merged change must preserve the repository's architectural boundaries: Harness owns routing, budgets, authorization, quality gates, recovery, and publication decisions; workers produce candidates only; replay never calls live dependencies; static Research remains the default. Existing durable event, checkpoint, artifact, budget, tool, and side-effect owners remain authoritative.

## Goals / Non-Goals

**Goals:**

- Deliver one production-shaped runtime path from admission through execution, child supervision, deterministic verification, join, parent continuation, and replay.
- Make execution capability, group/wave identity, budget, lease, receipt, artifact, and transcript references immutable and cross-checked before acceptance.
- Support bounded multi-pool parallelism, retry, replacement replan, cancellation, reclaim, indeterminate outcomes, and crash recovery without duplicate non-idempotent work.
- Preserve Research evidence, quality, reader/card, artifact, and publication gates while enabling an explicit dynamic-analysis opt-in.
- Produce focused test, architecture, golden-parity, deployment-capability, rollout, rollback, and replay evidence.

**Non-Goals:**

- Replacing the Graph compiler, durable event authority, side-effect authority, or existing Research quality gate.
- Introducing a distributed scheduler, unbounded queue, automatic autoscaling, or cross-run fairness.
- Enabling parallel execution by default or treating a feature flag as production qualification.
- Allowing workers to choose routing, permissions, retries, quality outcomes, memory/skill writes, or publication.

## Decisions

### 1. One merged capability, existing authorities retained

The new capability owns the cross-boundary contract and acceptance matrix. Existing stores and ports remain the only write authorities. This avoids a compatibility facade or duplicate runtime state while allowing the old changes' evidence to be migrated into one record.

Alternative: keep two active owners and duplicate handoff checks. Rejected because the same child admission and recovery path would have two definitions of safety and completion.

### 2. Admission before physical execution

Every child attempt is admitted with an immutable execution profile, exact Graph/group/wave/task/attempt identity, capability policy, budget reservation, input references, and transcript owner. The coordinator commits the spawn intent before calling the supervisor. Missing provider capabilities, refs, receipts, or policies fail closed.

Alternative: start the worker and validate its metadata afterward. Rejected because an unapproved or duplicate side effect would already have occurred.

### 3. Durable group and wave state

One `DispatchGroup` is the logical join scope. Capacity and dependency changes create bounded waves inside that group. Retry and replacement create new attempts or a new plan/group identity; late receipts from superseded groups are quarantined. Join order is stable and independent of completion order.

Alternative: create a new group for every retry or capacity slice. Rejected because parent continuation and required-role completeness would lose the original join semantics.

### 4. Deterministic verification and continuation

The result verifier checks identity, schema, transcript/output/artifact/tool receipt readability, memory and budget authority, and registered quality gates before accepting a child result. Parent continuation consumes only a gated durable observation with a terminal checksum; duplicate delivery reuses the same observation. Replay reads stored facts only.

Alternative: allow the worker or parent model to summarize raw child output directly. Rejected because it bypasses quality, redaction, and publication boundaries.

### 5. Explicit release states and rollback

The composition exposes `FEATURE_DISABLED`, `DEPENDENCY_UNAVAILABLE`, `DEGRADED_SERIAL`, and `ENABLED_PARALLEL`. The static default is disabled. New requests can be rolled back to disabled or an explicitly approved serial adapter; active groups retain their pinned policy, receipts, reservations, and history while they complete, cancel, reconcile, or halt.

Alternative: silently fall back to serial execution whenever capacity or a dependency is missing. Rejected because silent fallback hides a production qualification failure and can bypass required receipts or isolation.

## Risks / Trade-offs

- [External provider qualification] Local tests cannot prove Docker/process isolation or target rollback behavior -> record typed capability-blocked evidence and keep admission closed until a real provider receipt exists.
- [Large integration surface] Merging two mature changes can hide missing predecessor evidence -> migrate every unchecked predecessor task into a numbered merged task and require a traceability matrix.
- [Recovery ambiguity] A crash after spawn can leave side effects uncertain -> persist spawn intent and query supervisor status; use `INDETERMINATE` and quarantine rather than automatic retry when termination is unconfirmed.
- [Research regression] Parallel aggregation could change downstream semantics -> use fixed golden inputs with field-level role, evidence, gate, reader, card, and publication parity assertions.
- [Operational cost] Durable receipts and projections add latency and storage -> retain bounded references and metrics, while never dropping identity, checksums, or terminal evidence needed for replay.

## Migration Plan

1. Consolidate and validate the merged change, then remove the two predecessor directories as requested. This document migration is complete in `1f4a8ae2`; it does not qualify implementation or release.
2. Freeze the predecessor task/evidence snapshots and retain every still-open requirement and dependency in the merged traceability record. See `evidence/merged-traceability-20260923.md` and source snapshot `b13414cc2b64216c53fb4cb7f2ae76fb703a23ad`.
3. Keep the current static Research and single-child paths while implementing and testing the merged capability behind composition policy.
4. Validate the merged specs and run focused framework, Research, architecture, compile, full test, smoke, and source checks.
5. Run deployment capability and rollback qualification for every enabled provider; keep the feature disabled when evidence is missing. Preserve the predecessor provenance and current acceptance evidence in Git history.

Rollback disables new admissions or selects the explicitly approved serial adapter. It does not rewrite active group policy, delete receipts, bypass quality gates, or replay uncertain side effects.

## Open Questions

- Which deployment owner will provide the real provider isolation and rollback receipt required for release qualification?
- What retention period and artifact-store class will be used for complete parallel-run replay evidence in production?
- Which allowlisted Research tenants or environments will receive the first dynamic parallel rollout after G4 parity passes?
