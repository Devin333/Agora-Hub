# Verification

## Delivered scope

Native desktop chapter reader for published arXiv LaTeXML sources, with source reconstruction into typed blocks, complete source images (PNG and passive SVG), tables, equations, bibliography and appendices. PDF is auxiliary. No backend parser/OCR/model deployment or arbitrary archive upload is claimed.

## Current frontend validation

- `npm test`: 108 files, 580 tests passed.
- `npm run typecheck`: passed.
- `npm run lint`: passed with no warnings or errors.
- `npm run build`: passed, including the new dynamic reader and source-asset routes.
- `npx playwright test tests/e2e/reader-workspace.spec.ts --reporter=list`: 2 passed.
- `git diff --check -- frontend`: passed.
- `openspec validate paper-reader-workspace --strict`: passed.

## Real-source browser evidence

Paper: arXiv 2605.22343, Sibyl-AutoResearch.
The actual local reader was checked from its first section to Appendix F:
- 36 outline sections, over 80000 rendered text characters, 39 bibliography entries.
- All 7 source figures / 8 image panels loaded, including 2 SVG figures in the appendix.
- 12 native tables and inline KaTeX math; source block links navigate into references.
- No iframe or canvas in the default chapter body.
- Last PDF comparison page renders and is restored only when that auxiliary view is selected.
- Notes restore and export, font control and focus work, safe query return persists.
- No page-wide horizontal overflow at desktop widths 1366, 1440 and 1920.
- Dev service was stopped before production build and restarted on port 3000. Rechecked loaded styles and SVG on the restarted server.

Screenshots (local verification artifacts):
- `.newsroom/verification/reader-workspace/reader-fulltext-1440.png`
- `.newsroom/verification/reader-workspace/reader-evidence-1440.png`
- `.newsroom/verification/reader-workspace/reader-appendix-1440.png`

## Required Python smoke evidence and isolation

The shared checkout contains concurrent unrelated Harness/memory work. A clean isolated checkout at baseline `9fa80d16` was used for the required `F:/github/NewsRoom/.venv/Scripts/python.exe -m scripts.dev smoke`.
The ignored operations document required by the smoke was copied from the main checkout and its checksum checked before the final successful run.

Final process exit code: 0.
- pytest: 3116 passed, 23 deselected, 23 warnings, 1511.84 seconds.
- AgentLoop smoke: succeeded, network_calls=0.
- source validation: is_valid=true, error_count=0, warning_count=0.
- Log: `.newsroom/reader-workspace-isolated-smoke-rerun.log`.

This is baseline backend/architecture smoke evidence. Native source conversion was finalized afterward and is covered by the current frontend unit tests, browser E2E and production build above. No backend production files are part of this change; no claim is made that the unrelated dirty Harness/memory changes passed this isolated smoke.

## Availability limits

- Native conversion currently supports eligible arXiv LaTeXML HTML sources. Unsupported/no HTML source, image failure, title mismatch or incomplete source yields an unavailable/review state.
- A hard source/asset publication gate is intentional; it does not silently publish a visually incomplete paper as full text.
- AI analysis is not configured by this change. Real ask BFF and failure behavior are tested, but no successful live model answer is claimed.
- Notes are local browser data, not server sync.
