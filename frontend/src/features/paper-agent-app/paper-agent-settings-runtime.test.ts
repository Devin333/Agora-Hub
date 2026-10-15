import { afterEach, describe, expect, it, vi } from "vitest"
import { activateDesktopRestore, applyDesktopConfig, configDraftFromView, createDesktopBackup, hasDesktopConfigBridge, hasDesktopMaintenanceBridge, hasDesktopRuntimeBridge, inspectDesktopCredential, previewDesktopConfig, previewDesktopRestore, readDesktopHostSnapshot, removeDesktopCredential, replaceDesktopCredential, retryDesktopHostRuntime, showDesktopConfig, validateDesktopConfigDraft } from "./paper-agent-settings-runtime"

const status = { phase: "stopped" as const, state: "STOPPED", code: "runtime_stopped", message: "Stopped", canonicalOrigin: null, instanceKey: null, localTransportReady: false, canRetry: true, checkedAt: "2026-10-14T08:00:00Z" }
const diagnostics = { hostVersion: "1.2.3", packaged: true, runtime: status, recentEvents: [{ at: status.checkedAt, command: "status" as const, state: status.state, code: status.code, message: status.message }] }

afterEach(() => { delete (window as unknown as { agoraDesktop?: unknown }).agoraDesktop; vi.restoreAllMocks() })

describe("paper agent settings runtime bridge", () => {
  it("reports browser mode without inventing host capabilities", async () => {
    expect(hasDesktopRuntimeBridge()).toBe(false)
    await expect(readDesktopHostSnapshot()).resolves.toEqual({ kind: "browser" })
  })

  it("uses only the narrow status, retry and diagnostics bridge", async () => {
    const statusCall = vi.fn().mockResolvedValue(status)
    const retry = vi.fn().mockResolvedValue({ ...status, phase: "ready", state: "READY", code: "runtime_ready", canonicalOrigin: "http://127.0.0.1:3120", instanceKey: "local-desktop:http://127.0.0.1:3120", localTransportReady: true, canRetry: false })
    const diagnosticsCall = vi.fn().mockResolvedValue(diagnostics)
    Object.assign(window, { agoraDesktop: { version: 1, runtime: { status: statusCall, retry, diagnostics: diagnosticsCall } } })

    await expect(readDesktopHostSnapshot()).resolves.toMatchObject({ kind: "desktop", status: { phase: "stopped" } })
    await expect(retryDesktopHostRuntime()).resolves.toMatchObject({ kind: "desktop", status: { phase: "ready" } })
    expect(statusCall).toHaveBeenCalledTimes(1)
    expect(retry).toHaveBeenCalledTimes(1)
    expect(diagnosticsCall).toHaveBeenCalledTimes(2)
  })

  it("accepts the complete bounded host diagnostic command set", async () => {
    const extendedDiagnostics = { ...diagnostics, recentEvents: [{ ...diagnostics.recentEvents[0], command: "data-clear-status" as const }] }
    Object.assign(window, { agoraDesktop: { version: 1, runtime: { status: vi.fn().mockResolvedValue(status), retry: vi.fn(), diagnostics: vi.fn().mockResolvedValue(extendedDiagnostics) } } })

    await expect(readDesktopHostSnapshot()).resolves.toMatchObject({ kind: "desktop", status: { localTransportReady: false }, diagnostics: { recentEvents: [{ command: "data-clear-status" }] } })
  })

  it("projects the native backup, isolated preview and one-time activation contract", async () => {
    const backup = vi.fn().mockResolvedValue({ ok: true, state: "CREATED", code: "desktop_backup_created", message: "完成", backup: { backupId: "backup-1", fileName: "backup.zip", fileCount: 3, totalBytes: 42 } })
    const preview = vi.fn().mockResolvedValue({ ok: true, state: "VERIFIED", code: "desktop_restore_verified", message: "完成", previewId: "11111111-1111-4111-8111-111111111111", backupId: "backup-1", isolationVerified: true, expiresAt: "2026-10-14T08:05:00Z" })
    const activate = vi.fn().mockResolvedValue({ ok: true, state: "ACTIVATED", code: "desktop_restore_activated", message: "完成", backupId: "backup-1" })
    Object.assign(window, { agoraDesktop: { version: 1, runtime: { status: vi.fn(), retry: vi.fn(), diagnostics: vi.fn(), backup, restore: { preview, activate } } } })

    expect(hasDesktopMaintenanceBridge()).toBe(true)
    await expect(createDesktopBackup()).resolves.toMatchObject({ ok: true, backup: { fileName: "backup.zip" } })
    await expect(previewDesktopRestore()).resolves.toMatchObject({ isolationVerified: true })
    await expect(activateDesktopRestore("11111111-1111-4111-8111-111111111111")).resolves.toMatchObject({ state: "ACTIVATED" })
    expect(activate).toHaveBeenCalledWith({ previewId: "11111111-1111-4111-8111-111111111111" })
  })

  it("rejects malformed host payloads instead of displaying guessed health", async () => {
    Object.assign(window, { agoraDesktop: { version: 1, runtime: { status: vi.fn().mockResolvedValue({ phase: "ready" }), retry: vi.fn(), diagnostics: vi.fn() } } })
    await expect(readDesktopHostSnapshot()).rejects.toThrow()
  })
  it("requires the host transport verdict instead of guessing local readiness", async () => {
    const incomplete = { ...status } as Record<string, unknown>
    delete incomplete.localTransportReady
    Object.assign(window, { agoraDesktop: { version: 1, runtime: { status: vi.fn().mockResolvedValue(incomplete), retry: vi.fn(), diagnostics: vi.fn().mockResolvedValue(diagnostics) } } })

    await expect(readDesktopHostSnapshot()).rejects.toThrow()
  })

  it("adapts the exact typed config and boolean-only credential bridge", async () => {
    const configuration = { schema: "newsroom.desktop-runtime-settings/v1", revision: 4, research: { llm: { routeId: "writer-primary", provider: "openai-compatible", model: "gpt-test", baseUrl: "https://models.example/v1", credentialConfigured: true, credentialBinding: "windows-credential-manager" }, source: { provider: "arxiv", preset: "arxiv-default", apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } }
    const show = vi.fn().mockResolvedValue({ ok: true, state: "CONFIGURED", code: "read", message: "read", configuration })
    const preview = vi.fn().mockResolvedValue({ ok: true, state: "CONFIRMATION_REQUIRED", code: "preview", message: "preview", previewId: "opaque:preview", current: configuration, configuration: { ...configuration, revision: 5 }, effectScope: { llmConsumers: ["research"], sourceConsumers: ["research-arxiv-provider"], notChanged: ["guided-paper-search-public-cache"] } })
    const apply = vi.fn().mockResolvedValue({ ok: true, state: "APPLIED", code: "applied", message: "applied", configuration: { ...configuration, revision: 5 }, restartRequired: true, runtime: status })
    const statusCredential = vi.fn().mockResolvedValue({ ok: true, code: "configured", configured: true })
    const replace = vi.fn().mockResolvedValue({ ok: false, code: "cancelled", configured: true, cancelled: true })
    const remove = vi.fn().mockResolvedValue({ ok: true, code: "removed", configured: false, changed: true })
    Object.assign(window, { agoraDesktop: { version: 1, config: { show, preview, apply }, credentials: { status: statusCredential, replace, remove } } })

    expect(hasDesktopConfigBridge()).toBe(true)
    await expect(showDesktopConfig()).resolves.toMatchObject({ configuration: { revision: 4 } })
    const candidate = { research: { llm: { routeId: "writer-primary", model: "gpt-test", baseUrl: "https://models.example/v1" }, source: { apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } }
    await expect(previewDesktopConfig(candidate)).resolves.toMatchObject({ previewId: "opaque:preview" })
    await expect(applyDesktopConfig("opaque:preview")).resolves.toMatchObject({ state: "APPLIED", restartRequired: true, runtime: { phase: "stopped", localTransportReady: false } })
    expect(preview).toHaveBeenCalledWith(candidate)
    expect(apply).toHaveBeenCalledWith({ previewId: "opaque:preview" })
    await expect(inspectDesktopCredential()).resolves.toEqual({ ok: true, code: "configured", configured: true })
    await expect(replaceDesktopCredential()).resolves.toMatchObject({ cancelled: true })
    await expect(removeDesktopCredential()).resolves.toMatchObject({ configured: false })
  })

  it("validates renderer form fields without adding fixed host credential fields", () => {
    const view = { schema: "newsroom.desktop-runtime-settings/v1" as const, revision: 0, research: { llm: null, source: { provider: "arxiv" as const, preset: "arxiv-default" as const, apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } }
    expect(configDraftFromView(view)).toMatchObject({ llmEnabled: false, routeId: "writer-primary", sourceTimeoutSeconds: "90" })
    const invalid = validateDesktopConfigDraft({ llmEnabled: true, routeId: "bad route", model: "", baseUrl: "https://user:secret@example.test/v1", sourceApiUrl: "ftp://example.test", sourceTimeoutSeconds: "301" })
    expect(invalid.candidate).toBeUndefined()
    expect(invalid.errors).toEqual(expect.objectContaining({ routeId: expect.any(String), model: expect.any(String), baseUrl: expect.any(String), sourceApiUrl: expect.any(String), sourceTimeoutSeconds: expect.any(String) }))
    const valid = validateDesktopConfigDraft({ llmEnabled: true, routeId: "writer-primary", model: "gpt-test", baseUrl: "https://models.example/v1/", sourceApiUrl: "https://export.arxiv.org/api/query", sourceTimeoutSeconds: "45" })
    expect(valid.candidate).toEqual({ research: { llm: { routeId: "writer-primary", model: "gpt-test", baseUrl: "https://models.example/v1" }, source: { apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 45 } } })
    expect(JSON.stringify(valid.candidate)).not.toMatch(/credential|apiKey|secret/i)
  })

  it("rejects secret-bearing or malformed native projections", async () => {
    const show = vi.fn().mockResolvedValue({ ok: true, state: "CONFIGURED", code: "read", message: "read", configuration: { schema: "newsroom.desktop-runtime-settings/v1", revision: 1, research: { llm: { routeId: "writer-primary", provider: "openai-compatible", model: "gpt", baseUrl: "https://models.example/v1", credentialConfigured: true, credentialBinding: "windows-credential-manager", apiKey: "sk-secret" }, source: { provider: "arxiv", preset: "arxiv-default", apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } } })
    Object.assign(window, { agoraDesktop: { version: 1, config: { show, preview: vi.fn(), apply: vi.fn() } } })
    await expect(showDesktopConfig()).rejects.toThrow()
  })

})
