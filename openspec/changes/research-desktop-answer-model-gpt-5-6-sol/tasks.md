## 1. Dedicated deployment configuration

- [x] 1.1 Add a dedicated `gpt-5.6-sol` model group/deployment and repo/dev Research route without changing `writer-primary`.
- [x] 1.2 Document repo/dev Research selectors in `.env.example`; do not treat repo `.env` as Electron configuration.

## 2. Contract tests

- [x] 2.1 Test repo/dev deployment loading, model/base/key-env matching, and isolation from the generic writer route.
- [x] 2.2 Test structured-output capability remains unqualified and fail-closed.

## 3. Desktop admission repair and verification

- [x] 3.1 Record repository verification: targeted 100 passed, compile passed, strict validation passed, and smoke exited 0. These are not Electron apply evidence.
- [x] 3.2 Record Electron public preview success for `gpt-5.6-sol` and `https://deedee.tech/v1`, plus initial fail-closed apply result `desktop_runtime_config_release_required`; confirm persisted owner remains `revision=0`, `llm=null`, and credential remains unconfigured.
- [x] 3.3 Run config admission inside the desktop owned environment so inherited repo `NEWS_MODELS_CONFIG` cannot pollute release identity, while preserving admit-before-stop and fail-closed no-stop/no-write behavior.
- [x] 3.4 Add and pass targeted desktop regression tests for owned environment isolation, `admit -> stop -> apply` ordering, invalid confirmation, and failed release admission (`59 passed` across runtime/runtime-config/CLI/config tests).
- [x] 3.5 Record evidence boundaries: the model directory only proves `listed`; the framework client plain-text probe (`max_tokens=32`, `timeout=45`, `max_attempts=1`, `Reply exactly OK`) timed out once at 45 seconds and is not live-success evidence. Keep this separate from passing local tests.
- [x] 3.6 Run the required smoke gate and commit this bounded code repair; smoke exited 0 with 5224 passed, 1 skipped, 23 deselected; AgentLoop and source validation passed. Actual production activation remains pending below.

- [x] 3.7 Move optional Pillow loading to the Marker raster-validation use boundary; preserve all deterministic image security checks and add isolated import/no-false-success regressions (68 image/import tests and 56 desktop profile/bundle tests passed).
- [x] 3.8 Verify selected CPython 3.12.14 API import (174 routes, health present, PIL not loaded), pass 259 architecture and 124 image/desktop tests; required smoke r2 exited 0 (5224 passed, 1 skipped, 23 deselected, AgentLoop and source validation passed), then commit the dependency-boundary repair.

## 4. Production switch completion

- [x] 4.0 Verify a real remote completion through `OpenAICompatiblePaperAnswerModel`: requested/returned `gpt-5.6-sol`, grounded synthetic answer `24 participants.`, 6.75s, tokens 42/7. This is not Electron activation evidence.

- [x] 4.1 Rebuild and install the verified desktop installer from source revision `1784cf2cd81b9cc4e9072ff3e3b900b736edcace`; verify sealed install ownership and payload admission, and confirm all 85,061 user-data files retain identical paths, sizes, and SHA-256 hashes. This installs the bundle but does not by itself repair/replace the active production release.
- [x] 4.2 Verify the installed bundle with `PortableBundleManager.verify_bundle()` and activate its immutable runtime through the packaged `build` owner from the resource-root working directory; after config apply, the active config-bound release `20261022T054517Z-bc8d21da5385-portable-5492706d55b9-config-dafc28297a4e` is bound to source revision `1784cf2cd81b9cc4e9072ff3e3b900b736edcace`. Offline doctor verifies frontend closure and active production release; the previous release directory is retained.
- [x] 4.3 After explicit user authorization, provision the existing `.env` `OPENAI_API_KEY` through the installed desktop `WindowsCredentialVault` adapter into the fixed `paper-agent/research/openai-compatible` slot; credential-inspect reports configured without exposing the value.
- [x] 4.4 Apply the Electron owner configuration through preview/apply and the installed release owner; persisted revision 1 selects `research-desktop-answer` / `gpt-5.6-sol` at `https://deedee.tech/v1`, and the active config-bound release was activated.
- [x] 4.5 Verify persisted revision/model and resolve the active desktop API environment through the desktop owner credential projection: Research settings and the deployment both resolve to `gpt-5.6-sol`, with the fixed credential environment present (value never emitted); the live desktop API and frontend health checks pass. A separate synthetic completion using that exact owner-projected credential and installed model adapter returned `transient_network`, so remote inference success is not claimed.
