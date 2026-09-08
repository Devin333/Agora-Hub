# Verification

## Accepted scope

User approved the homepage UX plan and explicitly chose a locally saved, editable report preparation page; general-topic AI report generation backend is deferred. Desktop only. Existing Ask. Discover., purple glass, centered composer and 2x2 cards are retained.

## Requirement evidence

- Intent and direct entry: `entry.test.ts` covers project questions containing research, explicit/manual priority, ambiguous and unknown intent, all four URLs and unsafe URLs. Homepage tests cover direct navigation, retained input on errors and composition protection.
- All four destinations: browser submissions reached papers, projects, community and reports with original question. Papers returned published cached records. Projects and community returned explicit real-data empty states, not fabricated results. Project query editing wrote `q=Agent`; community source filtering wrote `source=reddit` while preserving `question` and `entry=home`.
- Research continuity: history and tracker tests cover bounded/invalid/blocked storage, delete/clear, URL filters, asynchronous height and StrictMode. Live browser resumed papers with `has=code&sort=most_cited` and scrollY=620; community resumed with source=reddit. Home draft survived ordinary navigation and reload.
- Report preparation: component and browser tests saved theme, scope, paper URL and notes, then reloaded and restored them. Unsafe URLs and storage failures retain content with error; API failure does not prevent local preparation. Lists and details no longer fall back to mock reports.
- Desktop and input: Playwright checks 1440x900 and 1366x768 composer center within 2px, 2x2 cards, mode keyboard navigation, Escape/outside dismissal, examples, login modal return, persisted draft, focus and IME. Browser screenshots saved under `.newsroom/verification/homepage-research-entry/`; no page errors in final screenshot session.

## Gates

- `openspec validate homepage-research-entry --strict`: passed.
- `git diff --check`: passed.
- `npm run typecheck`: passed.
- `npm run lint`: passed with an existing unrelated `open-reader-page.tsx:100` exhaustive-deps warning.
- `npm run build`: final run passed, including typecheck and all 37 static pages.
- `npm test`: final full suite passed, 104 files / 559 tests.
- `npx playwright test tests/e2e/homepage-research-entry.spec.ts`: 3 passed.
- `.venv/Scripts/python.exe -m scripts.dev smoke`: passed; 3114 tests passed, 23 deselected, 23 dependency/deprecation warnings; deterministic AgentLoop smoke succeeded, sources validation valid with zero errors/warnings.

## Boundaries

History and report preparation drafts are browser-local, not cloud-synchronized. No AI generation is simulated. Project/community data availability is an upstream condition surfaced honestly by the existing real endpoints. OAuth provider credentials and SMS/email sending remain the separate account configuration work. Unrelated concurrently staged Harness changes are excluded from this commit.
