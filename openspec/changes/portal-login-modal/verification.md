# Portal login verification

## Frontend

- Full Vitest suite: 97 files, 522 tests passed.
- Next.js production build: passed, including type checking and lint.
- Existing unrelated lint warning remains at `frontend/src/components/papers/open-reader/open-reader-page.tsx:100` (`expandRenderedSectionsTo`). No new lint warnings.
- Real local backend capability discovery reports all five methods unavailable until configured.
- Real desktop browser checks at 1366x768 and 1920x1080: centered dialog, phone/email switch, all three branded icons, unavailable-method messages, Escape dismissal and trigger focus restoration.
- Home and paper discovery keep URL, filters, and unsubmitted draft on dialog close.
- Full-page OAuth fallback context is covered by tests and a real page reload; the paper draft is restored after initialization under React StrictMode.
- A real local cross-site iframe verified that the first callback GET omits SameSite=Lax cookies, then the same-origin form POST carries the browser binding. The WeChat callback now uses that intermediate document and retains state and origin validation. This checks browser protocol behavior, not a real WeChat authorization.
- Corrected browser-facing origin comparison because Next's internal URL can use `localhost` when the incoming Host is `127.0.0.1`. Real same-origin BFF requests now reach the backend; foreign origins remain rejected.
- Local screenshots: `.tmp/portal-login-dialog.png`, `.tmp/portal-login-papers-desktop.png` (generated, not source-controlled).

## Activation boundary

No real Google/WeChat/QQ application, SMTP mailbox, or SMS credentials have been supplied. Provider exchanges and delivery are exercised with test transports; no real third-party login or outgoing OTP delivery is claimed. No fake QR, console OTP, or simulated production session is enabled.

## Backend and repository checks

- Final focused backend run: 39 tests passed, including public account service, real OAuth adapter validation with local signed JWTs, API routes, router parity, and OpenAPI contracts.
- Required `python -m scripts.dev smoke`: passed (3,078 tests, AgentLoop succeeded with zero network calls, sources validation valid with zero errors/warnings).
- Follow-up repository compile after final integration: passed.
- Strict OpenSpec validation: passed.
- Package metadata dry-run: passed using `E:/Anaconda3/python.exe -m pip install --dry-run --no-deps --no-build-isolation --no-index .`. The existing dependencies list was nested under `project.urls`; it now belongs to `project.dependencies` so the authentication dependencies install correctly.
- Backend test output includes Authlib's `jose` deprecation notice; dependency is constrained below version 2.0. Existing FastAPI/httpx deprecations also remain. No checks were weakened.
- The workspace also contains another task's changing Harness files. This delivery stages only portal authentication, its dependencies, documentation, and OpenSpec files.
