import { afterEach, describe, expect, it, vi } from "vitest"
import {
  assignModelConnections,
  createModelConnection,
  deleteModelConnection,
  discoverModels,
  fetchModelConnections,
  previewModelConnection,
  updateModelConnection,
  verifyModelConnection,
} from "./model-connections"

const view = {
  revision: 2,
  connections: [],
  assignments: { answer: null, evidence_summary: null, section_navigation: null, candidate_generation: null },
  purposes: [{ id: "answer", label: "回答", requirement: { inputTokens: 100, outputTokens: 10, structuredOutput: false } }],
  catalog: { providers: [] },
}

function respond(body: unknown, status = 200) {
  const fetch = vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }))
  vi.stubGlobal("fetch", fetch)
  return fetch
}

function lastCall(fetch: ReturnType<typeof respond>) {
  const [path, init] = fetch.mock.calls.at(-1) as unknown as [string, RequestInit]
  return { path, method: init.method ?? "GET", body: init.body === undefined ? undefined : JSON.parse(String(init.body)) }
}

describe("model connections client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("reads the account's connections and strips fields the contract does not know", async () => {
    const fetch = respond({ success: true, data: { ...view, sealed: "ciphertext" } })
    const read = await fetchModelConnections()
    expect(read).not.toHaveProperty("sealed")
    expect(lastCall(fetch)).toMatchObject({ path: "/api/v1/account/model-connections", method: "GET" })
  })

  it("sends only the fields a draft filled in, with the account revision", async () => {
    const fetch = respond({ success: true, data: view })
    await createModelConnection({ providerId: "deepseek", model: "deepseek-flash", apiKey: "sk-new", label: "", reasoningEffort: null }, 0)
    expect(lastCall(fetch)).toEqual({
      path: "/api/v1/account/model-connections",
      method: "POST",
      body: { providerId: "deepseek", model: "deepseek-flash", apiKey: "sk-new", expectedRevision: 0 },
    })

    await updateModelConnection("mc-0123456789abcdef", { providerId: "deepseek", model: "deepseek-flash", reasoningEffort: "high" }, 2)
    expect(lastCall(fetch)).toEqual({
      path: "/api/v1/account/model-connections/mc-0123456789abcdef",
      method: "PATCH",
      body: { providerId: "deepseek", model: "deepseek-flash", reasoningEffort: "high", expectedRevision: 2 },
    })
  })

  it("addresses a connection by its encoded id and deletes with the revision in the query", async () => {
    const fetch = respond({ success: true, data: view })
    await deleteModelConnection("mc/../x", 4)
    expect(lastCall(fetch)).toEqual({ path: "/api/v1/account/model-connections/mc%2F..%2Fx?expectedRevision=4", method: "DELETE", body: undefined })
    await verifyModelConnection("mc-0123456789abcdef")
    expect(lastCall(fetch)).toEqual({ path: "/api/v1/account/model-connections/mc-0123456789abcdef/verify", method: "POST", body: {} })
  })

  it("previews a draft with its settings only, never the key", async () => {
    const preview = {
      contextWindowTokens: 1_000_000, maxOutputTokens: 384_000, effectiveReasoningEffort: "high", structuredOutput: "json_object",
      fits: { answer: { fits: true, requiredWindow: 359_923, reason: null } },
    }
    const fetch = respond({ success: true, data: preview })

    await expect(previewModelConnection({ providerId: "deepseek", model: "deepseek-flash", apiKey: "sk-secret" })).resolves.toEqual(preview)
    expect(lastCall(fetch)).toEqual({
      path: "/api/v1/account/model-connections/preview",
      method: "POST",
      body: { providerId: "deepseek", model: "deepseek-flash" },
    })
  })

  it("assigns purposes and clears one with null", async () => {
    const fetch = respond({ success: true, data: view })
    await assignModelConnections({ answer: "mc-0123456789abcdef", evidence_summary: null }, 2)
    expect(lastCall(fetch)).toEqual({
      path: "/api/v1/account/model-connections/assignments",
      method: "PUT",
      body: { assignments: { answer: "mc-0123456789abcdef", evidence_summary: null }, expectedRevision: 2 },
    })
  })

  it("discovers models with a draft key or a stored connection's key", async () => {
    const fetch = respond({ success: true, data: { models: ["qwen3-max"] } })
    await expect(discoverModels({ providerId: "custom-openai-compatible", baseUrl: "https://gateway.example.com/v1", apiKey: "sk-draft" })).resolves.toEqual(["qwen3-max"])
    expect(lastCall(fetch).body).toEqual({ providerId: "custom-openai-compatible", baseUrl: "https://gateway.example.com/v1", apiKey: "sk-draft" })
    await discoverModels({ providerId: "custom-openai-compatible", baseUrl: "https://gateway.example.com/v1", connectionId: "mc-0123456789abcdef" })
    expect(lastCall(fetch).body).toEqual({ providerId: "custom-openai-compatible", baseUrl: "https://gateway.example.com/v1", connectionId: "mc-0123456789abcdef" })
  })
})
