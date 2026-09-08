## Context

The desktop homepage uses a centered composer and a 280px collapsible history sidebar. History has per-account revision-checked storage but tracks discovery URLs only; group selection is internal to the sidebar. Reader notebooks and report drafts are separate browser-local stores. Search modules already implement deterministic date/code/language filters, while source parsing exists in the Research application service.

## Goals / Non-Goals

**Goals:** Implement the full accepted efficiency plan: group-aware entry, real constraints, exact continuation, explicit scoped search, keyboard shortcuts, source entry, group materials, comparison/report reuse, prompt templates, archive and honest data ownership.

**Non-Goals:** Mobile layouts, third-party provider configuration, automated AI report-generation backend, generated substitute paper bodies, nested/shared groups, or changes to unrelated Harness work.

## Decisions

- Extend the existing validated account workspace instead of adding another independent persistence system. Add optional backward-readable fields for archived visits, progress, materials, report drafts and prompt templates; retain CAS revision protection, explicit guest import and owner clearing. Legacy optional fields normalize to empty values. Backend must enforce the same limits and owned references.
- Represent continuation separately from discovery URL: a typed paper-reader resume target contains paper identity, safe local reader path, section ID/title or PDF page, and original return URL. Report continuation contains the exact saved draft identity. A plain URL or LLM output cannot grant access to another account's material.
- Lift selected group into homepage state. Explicit removable destination sets `researchGroup` on new session URLs. A separate material-selection control determines input context. Selecting a storage group alone never silently injects documents into analysis.
- Use deterministic filter serialization shared by homepage and result pages. Show inferred filters as editable controls and remove recognized instruction framing from lexical queries. Paper dates/code/type and project activity/language/license/local-run support must reflect actual source metadata; absent metadata never passes a positive filter.
- Search defaults to all visible history with an explicit current-group scope option. A global keyboard-accessible palette searches history, groups, material titles/notes and drafts, opens modules and resumes records. Favorites can expand; archives stay searchable with clear archive state.
- Provide a group details surface linked from the sidebar. Persist stable material IDs, kind, source URL, optional internal reference, title and user notes. Selected materials feed deterministic side-by-side comparison and report preparation; show only source-backed values and user notes. Do not claim AI synthesis where no runtime exists.
- Detect supported source reference formats locally; resolve catalog/source metadata through a service. PDF uploads use a bounded authenticated job with durable ownership/status and the existing parser/compiler/gate. Reconcile retry by content identity and never expose a private uploaded document through public catalog routes.
- Keep homepage appearance stable. Extra filters and context controls appear only when relevant. Label restored drafts and provide clear/reset, reusable prompt templates and consistent text styling. Maintain full-screen reader without a new outer header.

## Risks / Trade-offs

- Larger account snapshots → bounded collections/byte budgets and batched writes; reject invalid or oversized data without eviction.
- Reader or report continuation across accounts → validate ownership before loading and do not populate account data from global legacy local storage automatically.
- Source URLs and PDF uploads → supported URL validation, no arbitrary private-network fetching, file/header/size checks, atomic artifacts, bounded processing and truthful failure states.
- Group context may be unavailable in a destination module → present selected references explicitly and wire only supported operations; never display a false "used sources" claim.
- Existing broad ignore rules omit some libraries/docs from clean checkouts → preserve them in isolated verification, record evidence and avoid bundling unrelated data.

## Migration Plan

Read prior history snapshots with normalized defaults, preserving titles/groups/favorites and deleted records. Keep original local keys for recovery. Offer explicit imports of guest materials/drafts/templates into authenticated ownership, deduplicate IDs and never overwrite existing account values implicitly. Deploy additive service/schema changes before the new frontend. Store rollout and real browser evidence with this change.
