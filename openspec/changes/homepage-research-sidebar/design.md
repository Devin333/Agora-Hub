## Context

The current Next.js homepage has a local ten-visit dropdown. Records contain module, question, URL and scroll position; IDs are derived from question text. Existing account authentication uses HttpOnly sessions through a Next.js proxy and a Python application service.

## Goals / Non-Goals

**Goals:** Ship a desktop research sidebar with usable navigation, searchable/grouped sessions, durable local migration and account isolation with honest sync status. Preserve homepage styling and resume behavior.

**Non-Goals:** Mobile/tablet drawers, nested/shared groups, drag sorting, AI-generated titles, research execution changes and third-party login provider setup.

## Decisions

- Use a 280px sticky sidebar (64px collapsed), independent scrolling and a flexible right workspace. Keep the 2x2 module tiles below the viewport-centered composer. Remove duplicate history entry points. Text-first rows and progressive menus keep the sidebar quieter than the glass content panels.
- Store an independent session UUID in the `researchSession` URL parameter for new homepage submissions. Preserve it through filter navigation. Migrate v1 question-derived IDs; subsequent filter changes keep one record. User-controlled titles are independent of questions.
- Version local storage to an object of `visits` and `groups`, with soft deletion and undo. Validate restored URLs, fields and group references. Preserve the legacy key as a migration fallback. No silent oldest-entry eviction; a bounded capacity produces an explicit storage error.
- Centralize account ownership and persistence in a client history provider/store shared by the tracker and sidebar. Guest records remain in their own storage. Account snapshots use separate memory, never fall back to guest data when loading fails. Account data is fetched on login and cleared on logout.
- Use GET/PUT `/api/research/history` via Next.js to `/api/v1/research/history`. A workspace snapshot contains visits, groups and a monotonically increasing revision. The backend authenticates the session, derives ownership and uses an atomic locked store with revision comparison. Conflicts cannot overwrite newer changes; the client retains unsaved changes and offers retry or explicit reload. No client-supplied owner is authoritative.
- Local-to-account import is explicit and idempotent by record IDs, preserves account conflicts, and leaves the guest copy intact. Non-conflicting updates refresh when the window regains focus; pending local edits are not silently discarded.

## Risks / Trade-offs

- Whole-workspace revision conflicts are coarser than per-record operations → retain local edits, explain conflicts and offer explicit reload; use bounded payloads and debounced writes.
- Browser storage can fail → surface failure rather than claiming saved; research navigation remains available.
- Account service may be unavailable → keep guest functionality, show authenticated loading/error state without leaking a previous account's history.
- URL state varies between modules → verify session continuity through real filter operations and preserve it in module URL builders.

## Migration Plan

Read and validate existing v1 entries when v2 is absent, preserve resume state, and write v2 on the first change. Never remove the old key during migration. The new account endpoint is additive; rollback restores the previous frontend without modifying old local records.

## Storage and operations

- `NEWSROOM_RESEARCH_HISTORY_PATH` overrides the default `.newsroom/research/history.json`. Keep this path on persistent storage alongside the existing account/session stores. The service uses the repository's file-lock and atomic-replace infrastructure; this is the existing single-host storage deployment model.
- Each workspace accepts at most 2,000 records (including deletion tombstones) and 100 groups. The Next.js proxy caps requests at 4 MiB; the client applies a slightly smaller byte limit before saving. Exceeding capacity produces an explicit failure without evicting older research.
- Guest history uses the versioned browser-local store. Account edits awaiting acknowledgement use account-specific session storage for recovery within the same browser tab. Sign-out removes the active account workspace from memory; guest records remain separate.
- A revision conflict requires an explicit recovery choice. Users can export the retained workspace before choosing to abandon unsynced changes and reload the latest server snapshot. There is no automatic last-writer-wins overwrite.
