## Verification

- Frontend full suite: 93 files, 486 tests passed.
- TypeScript check: passed.
- Focused ESLint: passed without warnings.
- Next.js production build: passed. An existing OpenReader effect dependency warning remains at open-reader-page.tsx:100; that implementation was not changed here.
- OpenSpec strict validation and scoped git diff whitespace check: passed.
- Required repository smoke: 3039 passed, 23 deselected; deterministic AgentLoop succeeded; source validation returned zero errors and warnings. Framework deprecation warnings remain unrelated to this frontend change.

## Browser Acceptance

Real local data, no mocked API responses. Desktop widths 1024, 1366 and 1920, height 900:

- No horizontal page overflow; PDF first-page canvas contains rendered nonblank pixels.
- At 1366x900, first paper starts near y=383 rather than y=577; the first two paper abstracts are visible.
- Query Agent + topic cs.AI + code availability intersects; query survives topic changes.
- Recent-period empty state identifies the latest available publication and all-time recovery retains keyword/topic/feature filters.
- Inclusive custom date range survives reload.
- Saved paper survives reload and appears in the reading list.
- Two selected papers produce a factual comparison table; removal updates the count and columns.
- Title preview closes with Escape; reader return preserves page two and restores scroll to the measured 472px position.
- Home paper question arrives verbatim in both original context and editable keyword input.
- No page errors and no missing Next.js static assets in the final workflow run.

Browser script and screenshots are local verification artifacts under .tmp/verify-papers-workflow.cjs and .tmp/papers-workflow-*.png, not production fixtures.

## Boundaries

Search remains deterministic keyword matching, not AI semantic retrieval. Reading selections remain browser-local. Comparison displays recorded source fields without generated judgments. The paper preview remains at /design-demo/papers; the legacy /papers layout is retained.
