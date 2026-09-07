## Context

The desktop preview delegates real paper loading to TrendingPapersPage. URL state and Zustand ID lists already exist, but topic selection overwrites q, selections have no destination, and reader links lose their origin.

## Goals / Non-Goals

Goals: complete discovery and local research workspace with the approved glass UI, explicit keyword matching, and deterministic state continuity.

Non-goals: semantic AI search, generated comparison verdicts, ingestion changes, account synchronization, or redesign of legacy /papers and the reader.

## Decisions

- Reuse shared filtering functions for API and client fallback. Independent q/topic/has/period/from/to parameters intersect; relevance ranks literal keyword matches. Existing sort modes remain available.
- Preserve the original home question in a separate context parameter. Never silently claim a generated semantic interpretation. The search field is explicitly keyword search.
- Add discovery/reading/compare views in the preview URL. Reading view includes both existing readingList and later IDs without destructive migration. One save action toggles membership in their union. Comparison uses source fields only and resolves missing IDs through the existing detail endpoint.
- Use explicit preview buttons for titles/covers, primary read/save actions, and secondary PDF/code/compare actions. Reader links carry allowlisted same-origin return paths; session storage retains scroll by path, restored after list loading.
- Retain category counts from the full published catalog and label them as such; show localized category names. Keep freshness separate from source errors and distinguish publication coverage from collection time.
- Compress heading and filter overhead, keeping a readable two-column desktop catalog and two paper summaries visible at 1366x900. Retain existing preview palette and glass surface.

## Risks / Trade-offs

- Local selections are device-only -> label the workspace scope and preserve IDs when records temporarily fail to load; allow retry/removal.
- Keyword search does not understand arbitrary questions -> retain original context and expose editable keyword query without semantic claims.
- Browser storage may be unavailable -> navigation still preserves filters and page; storage is best-effort.
- Corpus coverage is not completeness -> label maximum publication date as latest available, not proof of ingestion completeness.

## Migration Plan

Preview remains opt-in under /design-demo/papers. Shared query additions are backward-compatible. Existing saved/later IDs remain intact; rollback removes new views without deleting selections.
