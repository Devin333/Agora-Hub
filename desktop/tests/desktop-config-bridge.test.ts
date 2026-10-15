import { describe, expect, it, vi } from "vitest"
import { DesktopConfigBridge, validateConfigCandidate } from "../src/desktop-config-bridge"
import type { RuntimeCliResponse, RuntimeCommand, RuntimeStatusView } from "../src/contracts"

const candidate = {
  research: {
    llm: { routeId: "writer-primary", baseUrl: "https://api.example.test/v1/", model: "research-model" },
    source: { apiUrl: "https://export.arxiv.org/api/query?search=all", timeoutSeconds: 30 }
  }
}

const currentCandidate = {
  research: {
    llm: { routeId: "writer-primary", baseUrl: "https://api.example.test/v1/", model: "current-model" },
    source: { apiUrl: "https://export.arxiv.org/api/query?search=all", timeoutSeconds: 20 }
  }
}

function configuration(revision: number, projected = candidate) {
  return {
    schema: "newsroom.desktop-runtime-settings/v1",
    revision,
    research: {
      llm: projected.research.llm === null ? null : {
        route_id: projected.research.llm.routeId,
        provider: "openai-compatible",
        model: projected.research.llm.model,
        base_url: projected.research.llm.baseUrl.replace(/\/+$/, ""),
        credential_configured: false,
        credential_binding: "windows-credential-manager"
      },
      source: {
        preset: "arxiv-default",
        api_url: projected.research.source.apiUrl,
        timeout_seconds: projected.research.source.timeoutSeconds
      }
    }
  }
}

const effectScope = {
  llm_consumers: ["research", "guided-intent"],
  source_consumers: ["research-arxiv-provider"],
  not_changed: ["guided-paper-search-public-cache", "guided-project-search"]
}

const stoppedRuntime: RuntimeStatusView = {
  phase: "stopped", state: "STOPPED", code: "runtime_stopped", message: "Stopped", canonicalOrigin: null, instanceKey: null,
  localTransportReady: false, canRetry: true, checkedAt: "2026-10-15T08:00:00.000Z"
}

function response(command: RuntimeCommand, state: string, data: Record<string, unknown>, ok = true): RuntimeCliResponse {
  return { ok, command, state, code: `${command}_${ok ? "ok" : "failed"}`, message: "bounded protocol result", data }
}

function show(revision = 7, data: Record<string, unknown> = {}) {
  return response("config-show", "CONFIGURED", { configuration: configuration(revision, currentCandidate), effect_scope: effectScope, ...data })
}

function preview(revision = 7) {
  const digest = "b".repeat(64)
  return response("config-preview", "CONFIRMATION_REQUIRED", {
    configuration: configuration(revision + 1),
    effect_scope: effectScope,
    base_revision: revision,
    candidate_digest: digest,
    confirmation_token: `desktop-config:${"a".repeat(32)}:${digest.slice(0, 16)}`
  })
}

function applied(revision = 8, data: Record<string, unknown> = {}) {
  return response("config-apply", "APPLIED", { configuration: configuration(revision), effect_scope: effectScope, restart_required: true, ...data })
}

function fakeBridge(
  invoke: ReturnType<typeof vi.fn>,
  now: () => number = () => Date.parse("2026-10-14T09:00:00Z"),
  runtime: { refresh: () => Promise<RuntimeStatusView> } = { refresh: vi.fn().mockResolvedValue(stoppedRuntime) }
) {
  return new DesktopConfigBridge({ invoke } as never, runtime as never, now)
}

describe("DesktopConfigBridge protocol", () => {
  it("strictly projects show/preview and never exposes owner receipts or unknown fields", async () => {
    const invoke = vi.fn(async (command: RuntimeCommand) => command === "config-show" ? show() : preview())
    const bridge = fakeBridge(invoke)

    await expect(bridge.show()).resolves.toEqual({
      ok: true,
      state: "CONFIGURED",
      code: "config-show_ok",
      message: "bounded protocol result",
      configuration: {
        schema: "newsroom.desktop-runtime-settings/v1",
        revision: 7,
        research: {
          llm: {
            routeId: "writer-primary",
            provider: "openai-compatible",
            model: "current-model",
            baseUrl: "https://api.example.test/v1",
            credentialConfigured: false,
            credentialBinding: "windows-credential-manager"
          },
          source: { preset: "arxiv-default", provider: "arxiv", apiUrl: "https://export.arxiv.org/api/query?search=all", timeoutSeconds: 20 }
        }
      },
      effectScope: { llmConsumers: ["research", "guided-intent"], sourceConsumers: ["research-arxiv-provider"], notChanged: ["guided-paper-search-public-cache", "guided-project-search"] }
    })

    const projected = await bridge.preview(candidate)
    expect(projected).toMatchObject({ ok: true, state: "CONFIRMATION_REQUIRED", current: { revision: 7 }, configuration: { revision: 8 } })
    expect(projected.previewId).toMatch(/^[0-9a-f-]{36}$/i)
    expect(JSON.stringify(projected)).not.toMatch(/confirmation_token|candidate_digest|base_revision|desktop-config:/)

    const malformed = fakeBridge(vi.fn().mockResolvedValue(show(7, { configuration: { ...configuration(7), unexpected: true } })))
    await expect(malformed.show()).resolves.toMatchObject({ ok: false, state: "UNKNOWN", code: "desktop_config_show_protocol_invalid" })
  })

  it("keeps an opaque immutable candidate receipt and applies it once with host-only CAS material", async () => {
    let previewArgument: Record<string, unknown> | undefined
    let applyArgument: Record<string, unknown> | undefined
    const invoke = vi.fn(async (command: RuntimeCommand, options?: Record<string, unknown>) => {
      if (command === "config-show") return show()
      if (command === "config-preview") {
        previewArgument = options?.configCandidate as Record<string, unknown>
        ;(previewArgument.llm as Record<string, unknown>).model = "adapter-mutated-model"
        return preview()
      }
      applyArgument = options
      return applied()
    })
    const input = structuredClone(candidate)
    const runtime = { refresh: vi.fn().mockResolvedValue(stoppedRuntime) }
    const bridge = fakeBridge(invoke, undefined, runtime)
    const receipt = await bridge.preview(input)
    input.research.llm.model = "renderer-mutated-model"

    const lifecycle = vi.fn().mockResolvedValue(true)
    const confirm = vi.fn().mockResolvedValue(true)
    await expect(bridge.apply({ previewId: receipt.previewId }, lifecycle, confirm)).resolves.toMatchObject({ ok: true, state: "APPLIED", restartRequired: true, runtime: { phase: "stopped", localTransportReady: false } })
    expect(runtime.refresh).toHaveBeenCalledOnce()
    expect(applyArgument).toEqual({
      configCandidate: {
        llm: { route_id: "writer-primary", base_url: "https://api.example.test/v1", model: "research-model" },
        source: { api_url: "https://export.arxiv.org/api/query?search=all", timeout_seconds: 30 }
      },
      expectedRevision: 7,
      confirmationToken: `desktop-config:${"a".repeat(32)}:${"b".repeat(16)}`
    })
    expect(previewArgument).not.toBe(applyArgument?.configCandidate)
    await expect(bridge.apply({ previewId: receipt.previewId }, lifecycle, confirm)).resolves.toMatchObject({ ok: false, state: "CONFLICT" })
    expect(invoke.mock.calls.filter(call => call[0] === "config-apply")).toHaveLength(1)
  })

  it("expires receipts, rejects revision drift, and revalidates lifecycle after native confirmation", async () => {
    let now = Date.parse("2026-10-14T09:00:00Z")
    const invoke = vi.fn(async (command: RuntimeCommand) => command === "config-show" ? show() : command === "config-preview" ? preview() : applied())
    const bridge = fakeBridge(invoke, () => now)
    const expired = await bridge.preview(candidate)
    now += 15 * 60_000
    await expect(bridge.apply({ previewId: expired.previewId }, vi.fn(), vi.fn())).resolves.toMatchObject({ ok: false, state: "CONFLICT" })

    now = Date.parse("2026-10-14T10:00:00Z")
    const revisionInvoke = vi.fn()
      .mockResolvedValueOnce(show(7)).mockResolvedValueOnce(preview(7)).mockResolvedValueOnce(show(8))
    const revisionBridge = fakeBridge(revisionInvoke, () => now)
    const stale = await revisionBridge.preview(candidate)
    await expect(revisionBridge.apply({ previewId: stale.previewId }, vi.fn(), vi.fn())).resolves.toMatchObject({ ok: false, state: "CONFLICT" })
    expect(revisionInvoke.mock.calls.map(call => call[0])).not.toContain("config-apply")

    const freshInvoke = vi.fn(async (command: RuntimeCommand) => command === "config-show" ? show() : command === "config-preview" ? preview() : applied())
    const freshBridge = fakeBridge(freshInvoke, () => now)
    const fresh = await freshBridge.preview(candidate)
    const lifecycle = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false)
    await expect(freshBridge.apply({ previewId: fresh.previewId }, lifecycle, vi.fn().mockResolvedValue(true))).resolves.toMatchObject({ ok: false, state: "CANCELLED", cancelled: true })
    expect(lifecycle).toHaveBeenCalledTimes(2)
    expect(freshInvoke.mock.calls.map(call => call[0])).not.toContain("config-apply")
  })

  it("keeps an authoritative APPLIED result when the post-apply status refresh is unavailable", async () => {
    const invoke = vi.fn(async (command: RuntimeCommand) => command === "config-show" ? show() : command === "config-preview" ? preview() : applied())
    const runtime = { refresh: vi.fn().mockRejectedValue(new Error("status unavailable")) }
    const bridge = fakeBridge(invoke, undefined, runtime as never)
    const receipt = await bridge.preview(candidate)

    const result = await bridge.apply({ previewId: receipt.previewId }, vi.fn().mockResolvedValue(true), vi.fn().mockResolvedValue(true))
    expect(result).toMatchObject({ ok: true, state: "APPLIED", restartRequired: true })
    expect(result).not.toHaveProperty("runtime")
    expect(runtime.refresh).toHaveBeenCalledOnce()
  })

  it("never reports APPLIED without authoritative configuration and maps API errors to UNKNOWN", async () => {
    const missingConfig = vi.fn(async (command: RuntimeCommand) => command === "config-show" ? show() : command === "config-preview" ? preview() : response("config-apply", "APPLIED", { restart_required: true }))
    const bridge = fakeBridge(missingConfig)
    const receipt = await bridge.preview(candidate)
    await expect(bridge.apply({ previewId: receipt.previewId }, vi.fn().mockResolvedValue(true), vi.fn().mockResolvedValue(true))).resolves.toEqual({
      ok: false,
      state: "UNKNOWN",
      code: "desktop_config_apply_protocol_invalid",
      message: "配置事务回执无法确认；请重新读取配置，不得假称成功。"
    })

    const apiError = fakeBridge(vi.fn().mockResolvedValue(response("config-show", "ERROR", { configuration: configuration(99), effect_scope: effectScope }, false)))
    const result = await apiError.show()
    expect(result).toEqual({ ok: false, state: "UNKNOWN", code: "config-show_failed", message: "bounded protocol result" })
    expect(result).not.toHaveProperty("configuration")
  })

  it("matches the bounded URL and model validation contract", () => {
    const prefix = "https://example.test/"
    const exactUrl = prefix + "a".repeat(2048 - prefix.length)
    expect(validateConfigCandidate({ ...candidate, research: { ...candidate.research, source: { apiUrl: exactUrl, timeoutSeconds: 30 } } }).source.api_url).toHaveLength(2048)
    expect(() => validateConfigCandidate({ ...candidate, research: { ...candidate.research, source: { apiUrl: `${exactUrl}a`, timeoutSeconds: 30 } } })).toThrow(/api_url_invalid/)
    expect(validateConfigCandidate({ ...candidate, research: { ...candidate.research, llm: { ...candidate.research.llm, model: "m".repeat(256) } } }).llm?.model).toHaveLength(256)
    expect(() => validateConfigCandidate({ ...candidate, research: { ...candidate.research, llm: { ...candidate.research.llm, model: "m".repeat(257) } } })).toThrow(/model_invalid/)
    expect(() => validateConfigCandidate({ ...candidate, research: { ...candidate.research, llm: { ...candidate.research.llm, model: "Bearer secret" } } })).toThrow(/model_invalid/)
  })
})

