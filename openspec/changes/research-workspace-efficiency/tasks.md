## 1. Unified owned workspace

- [x] 1.1 Extend validated workspace model and backward normalization for archive, exact activity, materials, report drafts and saved prompts.
- [x] 1.2 Extend authenticated atomic persistence, input limits and CAS tests for the same model; preserve guest/account isolation and explicit import.
- [x] 1.3 Move report draft and homepage draft ownership into the workspace flow with honest legacy-local import and save status.

## 2. Faster entry and navigation

- [x] 2.1 Connect selected group and removable composer destination to new session persistence; separate selected materials from storage destination.
- [x] 2.2 Implement deterministic paper/project constraints, recognized editable conditions and actual destination filtering/active summaries.
- [x] 2.3 Implement explicit search scope, cross-module command palette, favorite shortcuts and reversible archive navigation.
- [x] 2.4 Add restored draft state, clear/continue, saved prompt management and consistent desktop typography.

## 3. Research continuity and materials

- [x] 3.1 Persist exact reader activity and restore section/PDF page without crossing owners or changing body publication rules.
- [x] 3.2 Persist exact report continuation and selected materials; surface activity in history without duplicate homepage history cards.
- [x] 3.3 Build group materials/detail surface, source/reference/notes CRUD and ownership-aware direct module save actions.
- [x] 3.4 Implement selected-material comparison, explicit context reuse and one-step report preparation with real source attribution.

## 4. Source entry

- [x] 4.1 Recognize and resolve supported arXiv/DOI/GitHub references with reader, related-search and group actions.
- [x] 4.2 Implement authenticated bounded PDF import and durable status/retry through the existing parsing/publication pipeline.
- [x] 4.3 Add homepage source/attachment UI with true loading/error/status and native-reader completion path.

## 5. Verification and delivery

- [x] 5.1 Run focused ownership, migration, filters, continuation, import and UI tests plus full frontend tests/typecheck/lint/build.
- [ ] 5.2 Verify real desktop browser workflows with sources, accounts, groups, resume, search and reports; record evidence and capability limits.
  - Verified in a headless desktop browser: homepage HTTP 200, page title, main heading and Ctrl+K dialog opening. Account switching, PDF import, group/material reuse, exact resume and report preparation have automated test coverage but have not been individually verified end-to-end in a browser.
- [ ] 5.3 Run strict OpenSpec validation and required smoke; repair failures, audit every requirement, commit scoped changes and keep the local app usable.
  - Strict OpenSpec validation, scoped backend tests, frontend tests/typecheck/lint/build and required smoke passed. Implementation is committed in 605b0bad and the local homepage returns HTTP 200. Final acceptance remains pending the browser workflows in task 5.2.
