# Homepage research sidebar verification

Date: 2026-09-08

## Scope

Desktop homepage sidebar, validated research history, groups, favorites, resume URLs, guest migration and authenticated account persistence. Existing unrelated Harness/Research work remains outside this change.

## Frontend

- `npm run test`: 113 files, 605 tests passed.
- `npm run typecheck`: passed.
- `npm run lint`: passed, no warnings.
- `npm run build`: passed; 37 static pages generated. The production build ran in `F:/github/NewsRoom-sidebar-verification-20260908` to avoid modifying the live development server's `.next` cache.
- The verification checkout uses the existing dependency installation. The repository's existing broad `lib/` ignore rule excludes several feature library directories from Git; those unchanged directories were copied from the live workspace for the build. This verifies the actual workspace frontend, not clean-clone dependency completeness.
- Sync regression coverage includes stale replies after account switches, writes while another write is in flight, conflict preservation, pending draft recovery after reload, explicit discard/reload without an overwrite request, and preventing unfinished scroll restoration from crossing account ownership.

## Real browser

Chromium against the running development frontend at `http://127.0.0.1:3000/design-demo`:

- At 1280 x 800, expanded sidebar width is 280px. Composer bounds are x=320, y=300, width=920, height=200. Its center is exactly the remaining workspace center horizontally and viewport center vertically.
- With the sidebar collapsed to 64px, composer bounds are x=192, y=300, width=960, height=200. No horizontal overflow in either state. Also visually checked at 1440 x 1000.
- The four module cards remain in a 2 x 2 grid below the composer; homepage content and history list scroll independently.
- Created a group, submitted a real paper query, changed sorting to `most_cited`, returned home and resumed. The original `researchSession`, query and sort remain in the URL.
- Verified moving a study to a group, favoriting, deleting, undoing deletion, empty search feedback, clearing search and collapsing/expanding the sidebar.
- Screenshots: `.newsroom/sidebar-home-expanded.png` and `.newsroom/sidebar-home-collapsed.png` (local verification artifacts).

## Account integration

A dedicated API on port 8018 uses the real account and history application services with isolated data under `.newsroom/sidebar-e2e`. A separate browser context forwards its session/history requests to that API. This exercises the actual UI and backend persistence without adding test accounts to the user's data. Next.js cookie forwarding, origin validation and HTTP status propagation are covered separately by proxy tests.

- Focused backend service/API tests: 13 passed, including two actual account sessions, atomic competing writes, data validation and conflict rejection.

- Real bootstrap/session authentication succeeded.
- Guest records were absent from the account until clicking "合并到账号". The actual history PUT returned 200; records and grouping persisted after reload. The import prompt disappeared after the successful idempotent import.
- A direct competing save advanced the server revision. Renaming in the stale browser produced a real 409. The browser retained its unsaved title; the server retained the competing title.
- "加载最新记录" required a confirmation. After confirmation, the latest server title appeared and status returned to synced.
- Third-party provider setup is outside this change. Existing configured authentication sessions are used; no fake login or automatic guest-to-account conversion is introduced.
- The existing port-8000 development backend was restarted to load the new route. Both direct API and port-3000 proxy now return the expected unauthenticated 401 (previously the old backend returned 404). The actual homepage was reloaded successfully after the restart. An existing optional Qdrant preload reported 502; startup and account/history requests remained available, and search infrastructure was not changed.

## Delivery gates

- `openspec validate homepage-research-sidebar --strict`: passed.
- Required `python -m scripts.dev smoke` was run against the isolated task snapshot. Python compilation passed. The regression phase finished with 3,170 passed, 23 deselected by existing configuration, and one failure: `docs/operations/agent-session-retirement.md` was absent from the checkout because the existing `docs/` rule ignores it.
- Corrected the verification workspace by copying the exact existing operations note from the main workspace; no production code, test assertion or test selection was weakened. Re-ran the failed architecture test: 1 passed. The other 3,170 previously passing tests were not repeated for this documentation-only environment repair. This is combined verification evidence, not a claim that the original smoke process exited successfully.
- Completed the two downstream commands that the first failure had prevented: `python -m scripts.dev smoke-test-agent-loop` passed (`status=succeeded`, `network_calls=0`); `python -m scripts.dev sources-validate` passed (`is_valid=true`, zero errors/warnings).
- All requested checks have passing evidence after the environment repair. The live workspace's unrelated Harness modifications remain unstaged. The final commit contains only the homepage/sidebar/account-history change and its specification/tests.
- An additional port-3001 production preview launch was rejected by automatic approval review with `blocked by policy`. That process was not started. Verification used the running port-3000 frontend and the existing isolated API instead.
