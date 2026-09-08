## Why

The homepage's ten-entry local dropdown duplicates the resume strip and cannot organize ongoing research. Users need a persistent desktop history sidebar with topic groups, recoverable actions, and account-owned storage.

## What Changes

- Replace the homepage history dropdown and resume strip with a collapsible sidebar; center the composer in the remaining workspace.
- Add history search, date sections, favorites, one-level groups, rename, move, delete and undo.
- Give new research independent session identity while filter changes update the existing session. Migrate existing local history without losing resume URLs or positions.
- Persist authenticated workspaces through an account-authorized, versioned service. Offer explicit local-history import and surface sync errors and conflicts.
- Keep the established homepage typography, purple accents, glass module tiles and desktop-only layout.

## Capabilities

### New Capabilities
- `research-history-workspace`: Desktop sidebar, durable research sessions, grouping and account-scoped synchronization.

### Modified Capabilities
None.

## Impact

Frontend research history, tracker, homepage and account integration; new account research-history application service and API; scoped frontend/backend tests. No changes to research execution or Harness behavior. No new external providers are required.
