import { NextRequest } from "next/server"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { desktopPost as authenticatedDesktopPost } from "@/lib/research/desktop/server-proxy"
import { sanitizePersonalMemoryIndexRebuild } from "@/lib/research/personal-memory-contracts"

const session = vi.hoisted(() => ({ token: "owner-a-session" as string | undefined }))
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => session.token ? { value: session.token } : undefined }),
}))

const proxy = vi.hoisted(() => ({
  desktopFailure: vi.fn(() => new Response(null, { status: 400 })),
  desktopGet: vi.fn(() => new Response(null, { status: 204 })),
  desktopPost: vi.fn<(
    path: string,
    request: NextRequest,
    sanitize: (value: Record<string, unknown>) => Record<string, unknown> | null,
  ) => Response | Promise<Response>>(() => new Response(null, { status: 204 })),
  personalMemoryPatch: vi.fn(() => new Response(null, { status: 204 })),
  personalMemoryPut: vi.fn(() => new Response(null, { status: 204 })),
  personalMemoryDelete: vi.fn(() => new Response(null, { status: 204 })),
}))
vi.mock("./_server", () => proxy)

import { GET as listMemories, POST as createMemory } from "./route"
import { GET as exportMemories } from "./export/route"
import { GET as memoryInfo } from "./info/route"
import { GET as getPolicy, PUT as updatePolicy } from "./policy/route"
import { POST as recallMemories } from "./recall/route"
import { POST as rebuildMemoryIndex } from "./rebuild-index/route"
import { DELETE as deleteMemory, GET as getMemory, PATCH as updateMemory } from "./[memoryId]/route"
import { POST as confirmMemory } from "./[memoryId]/confirm/route"
import { POST as updateStatus } from "./[memoryId]/status/route"

describe("personal memory BFF route mapping", () => {
  const memoryId = `pmem_${"a".repeat(32)}`
  beforeEach(() => vi.clearAllMocks())

  it("forwards bounded list/export/info/policy paths", async () => {
    const listRequest = request("/api/research/personal-memory?limit=20&status=active")
    await listMemories(listRequest)
    expect(proxy.desktopGet).toHaveBeenCalledWith(
      "/api/v1/research/desktop/memory?limit=20&status=active",
      listRequest,
    )

    const invalid = request("/api/research/personal-memory?user_id=forged")
    await listMemories(invalid)
    expect(proxy.desktopFailure).toHaveBeenCalledWith("invalid_request", 400)

    const exportRequest = request("/api/research/personal-memory/export?include_deleted=false")
    await exportMemories(exportRequest)
    expect(proxy.desktopGet).toHaveBeenCalledWith(
      "/api/v1/research/desktop/memory/export?include_deleted=false",
      exportRequest,
    )
    const infoRequest = request("/api/research/personal-memory/info")
    await memoryInfo(infoRequest)
    expect(proxy.desktopGet).toHaveBeenCalledWith("/api/v1/research/desktop/memory/info", infoRequest)
    const policyRequest = request("/api/research/personal-memory/policy")
    await getPolicy(policyRequest)
    expect(proxy.desktopGet).toHaveBeenCalledWith("/api/v1/research/desktop/memory/policy", policyRequest)
  })

  it("maps every mutation to the owner-scoped backend endpoint", async () => {
    const createRequest = request("/api/research/personal-memory", "POST")
    await createMemory(createRequest)
    expect(proxy.desktopPost).toHaveBeenCalledWith(
      "/api/v1/research/desktop/memory",
      createRequest,
      expect.any(Function),
    )

    const recallRequest = request("/api/research/personal-memory/recall", "POST")
    await recallMemories(recallRequest)
    expect(proxy.desktopPost).toHaveBeenCalledWith(
      "/api/v1/research/desktop/memory/recall",
      recallRequest,
      expect.any(Function),
    )

    const policyRequest = request("/api/research/personal-memory/policy", "PUT")
    await updatePolicy(policyRequest)
    expect(proxy.personalMemoryPut).toHaveBeenCalledWith(
      "/api/v1/research/desktop/memory/policy",
      policyRequest,
      expect.any(Function),
    )

    const props = { params: Promise.resolve({ memoryId }) }
    const itemRequest = request(`/api/research/personal-memory/${memoryId}`)
    await getMemory(itemRequest, props)
    expect(proxy.desktopGet).toHaveBeenCalledWith(`/api/v1/research/desktop/memory/${memoryId}`, itemRequest)
    await updateMemory(request(`/api/research/personal-memory/${memoryId}`, "PATCH"), props)
    expect(proxy.personalMemoryPatch).toHaveBeenCalledWith(
      `/api/v1/research/desktop/memory/${memoryId}`,
      expect.any(NextRequest),
      expect.any(Function),
    )
    await deleteMemory(request(`/api/research/personal-memory/${memoryId}`, "DELETE"), props)
    expect(proxy.personalMemoryDelete).toHaveBeenCalledWith(
      `/api/v1/research/desktop/memory/${memoryId}`,
      expect.any(NextRequest),
      expect.any(Function),
    )
    await confirmMemory(request(`/api/research/personal-memory/${memoryId}/confirm`, "POST"), props)
    expect(proxy.desktopPost).toHaveBeenCalledWith(
      `/api/v1/research/desktop/memory/${memoryId}/confirm`,
      expect.any(NextRequest),
      expect.any(Function),
    )
    await updateStatus(request(`/api/research/personal-memory/${memoryId}/status`, "POST"), props)
    expect(proxy.desktopPost).toHaveBeenCalledWith(
      `/api/v1/research/desktop/memory/${memoryId}/status`,
      expect.any(NextRequest),
      expect.any(Function),
    )
  })

  it("rejects invalid path ids before proxying", async () => {
    await getMemory(request("/api/research/personal-memory/escape"), {
      params: Promise.resolve({ memoryId: "../../escape" }),
    })
    expect(proxy.desktopFailure).toHaveBeenCalledWith("invalid_request", 400)
    expect(proxy.desktopGet).not.toHaveBeenCalled()
  })
})

function request(path: string, method = "GET") {
  return new NextRequest(`http://127.0.0.1:3000${path}`, { method })
}


describe("personal memory index rebuild authenticated BFF boundary", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    session.token = "owner-a-session"
    vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", "local-desktop")
    vi.stubEnv("NEWSROOM_DESKTOP_CANONICAL_ORIGIN", "http://127.0.0.1:4317")
    vi.stubEnv("NEWSROOM_API_BASE_URL", "http://127.0.0.1:4318")
    proxy.desktopPost.mockImplementation(authenticatedDesktopPost)
  })

  afterEach(() => {
    proxy.desktopPost.mockReset().mockImplementation(() => new Response(null, { status: 204 }))
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it("rejects unauthenticated requests without contacting the backend", async () => {
    const backend = vi.fn()
    vi.stubGlobal("fetch", backend)
    session.token = undefined

    const response = await rebuildMemoryIndex(rebuildRequest({}))

    expect(response.status).toBe(401)
    expect(await response.json()).toEqual({
      success: false,
      error: { code: "auth_required", message: "个人记忆索引重建失败。" },
    })
    expect(response.headers.get("cache-control")).toBe("private, no-store")
    expect(backend).not.toHaveBeenCalled()
  })

  it("rejects cross-origin requests before forwarding even with a valid session", async () => {
    const backend = vi.fn()
    vi.stubGlobal("fetch", backend)

    const response = await rebuildMemoryIndex(rebuildRequest({}, { origin: "https://foreign.invalid" }))

    expect(response.status).toBe(403)
    expect(await response.json()).toMatchObject({ success: false, error: { code: "auth_invalid_origin" } })
    expect(backend).not.toHaveBeenCalled()
  })

  it.each([
    { owner: "forged-owner" },
    { user_id: "forged-user" },
    { tenant_id: "forged-tenant" },
    { namespace: "foreign-namespace" },
    { revision: 1 },
    { force: true },
    [],
    null,
  ].map(body => ({ body })))("rejects nonempty or nonobject rebuild input %j before forwarding", async ({ body }) => {
    const backend = vi.fn()
    vi.stubGlobal("fetch", backend)

    const response = await rebuildMemoryIndex(rebuildRequest(body))

    expect(response.status).toBe(400)
    expect(await response.json()).toMatchObject({ success: false, error: { code: "invalid_request" } })
    expect(backend).not.toHaveBeenCalled()
  })

  it.each(["{invalid-json", "", "x".repeat(256 * 1024 + 1)])(
    "rejects malformed, empty, or oversized request bodies without forwarding",
    async body => {
      const backend = vi.fn()
      vi.stubGlobal("fetch", backend)
      const response = await rebuildMemoryIndex(rebuildRequest(body, { raw: true }))
      expect(response.status).toBe(400)
      expect(backend).not.toHaveBeenCalled()
    },
  )

  it("forwards only the server session and strict empty body, then returns a whitelisted result", async () => {
    const backend = vi.fn().mockResolvedValue(upstreamJson({
      success: true,
      data: { indexed_records: 7, owner: "foreign-owner", api_key: "data-secret" },
      api_key: "root-secret",
      debug: { token: "debug-secret" },
    }, 200, { "X-Provider-Key": "header-secret", "Set-Cookie": "provider=secret", "Content-Disposition": "attachment; filename=secret" }))
    vi.stubGlobal("fetch", backend)
    const input = rebuildRequest({})
    const response = await rebuildMemoryIndex(input)

    expect(proxy.desktopPost).toHaveBeenCalledWith(
      "/api/v1/research/desktop/memory/rebuild-index",
      input,
      sanitizePersonalMemoryIndexRebuild,
    )
    expect(backend).toHaveBeenCalledTimes(1)
    const [url, init] = backend.mock.calls[0] as [string, RequestInit]
    expect(url).toBe("http://127.0.0.1:4318/api/v1/research/desktop/memory/rebuild-index")
    expect(init).toMatchObject({ method: "POST", body: "{}", cache: "no-store" })
    const headers = new Headers(init.headers)
    expect(headers.get("X-Newsroom-Session")).toBe("owner-a-session")
    expect(headers.get("Origin")).toBe("http://127.0.0.1:4317")
    expect(headers.has("Authorization")).toBe(false)
    expect(response.status).toBe(200)
    expect(response.headers.get("cache-control")).toBe("private, no-store")
    expect(response.headers.get("referrer-policy")).toBe("no-referrer")
    expect(response.headers.get("x-content-type-options")).toBe("nosniff")
    expect(response.headers.has("set-cookie")).toBe(false)
    expect(response.headers.has("x-provider-key")).toBe(false)
    expect(response.headers.has("content-disposition")).toBe(false)
    expect(await response.json()).toEqual({ success: true, data: { indexed_records: 7 } })
  })

  it("uses the current server session for each owner rather than caller headers", async () => {
    const backend = vi.fn().mockImplementation(() => Promise.resolve(upstreamJson({
      success: true, data: { indexed_records: 0 },
    })))
    vi.stubGlobal("fetch", backend)
    await rebuildMemoryIndex(rebuildRequest({}))
    session.token = "owner-b-session"
    const second = await rebuildMemoryIndex(rebuildRequest({}))

    expect(new Headers(backend.mock.calls[0][1].headers).get("X-Newsroom-Session")).toBe("owner-a-session")
    expect(new Headers(backend.mock.calls[1][1].headers).get("X-Newsroom-Session")).toBe("owner-b-session")
    expect(await second.json()).toEqual({ success: true, data: { indexed_records: 0 } })
  })

  it("preserves upstream failure status but never returns error extras or provider messages", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstreamJson({
      success: false,
      error: {
        code: "personal_memory_store_unavailable",
        message: "provider-key-must-not-leak",
        details: { api_key: "private-key", owner: "foreign-owner" },
        requestId: "private-request-debug",
      },
      data: { indexed_records: 99 },
      debug: "private-debug",
    }, 503)))

    const response = await rebuildMemoryIndex(rebuildRequest({}))

    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({
      success: false,
      error: { code: "personal_memory_store_unavailable", message: "个人记忆索引重建失败。" },
    })
  })

  it.each(["provider_secret_key", "private-key:sk-secret", null])("does not disclose unknown upstream error codes %j", async code => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstreamJson({
      success: false,
      error: { code, message: "private-upstream-message" },
    }, 500)))

    const response = await rebuildMemoryIndex(rebuildRequest({}))

    expect(response.status).toBe(500)
    expect(await response.json()).toEqual({
      success: false,
      error: { code: "research_service_unavailable", message: "个人记忆索引重建失败。" },
    })
  })

  it("reports network unavailability without claiming the index was rebuilt", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("provider-key-in-network-diagnostics")))
    const response = await rebuildMemoryIndex(rebuildRequest({}))
    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({
      success: false,
      error: { code: "research_service_unavailable", message: "个人记忆索引重建失败。" },
    })
  })

  it.each([
    { success: true, data: { indexed_records: -1 } },
    { success: true, data: { indexed_records: 1.5 } },
    { success: true, data: { indexed_records: "7" } },
    { success: true, data: { indexed_records: Number.MAX_SAFE_INTEGER + 1 } },
    { success: true, data: {} },
    { success: false, data: { indexed_records: 7 } },
    { indexed_records: 7 },
    null,
  ])("fails closed on malformed successful upstream results %j", async payload => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstreamJson(payload)))
    const response = await rebuildMemoryIndex(rebuildRequest({}))
    expect(response.status).toBe(502)
    expect(await response.json()).toMatchObject({ success: false, error: { code: "invalid_upstream_response" } })
  })

  it("fails closed on non-JSON successful upstream responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private-non-json-debug", { status: 200 })))
    const response = await rebuildMemoryIndex(rebuildRequest({}))
    expect(response.status).toBe(502)
    expect(await response.json()).toEqual({
      success: false,
      error: { code: "invalid_upstream_response", message: "个人记忆服务返回了无效的索引重建结果。" },
    })
  })
})

function rebuildRequest(body: unknown, options: { origin?: string; raw?: boolean } = {}) {
  return new NextRequest("http://127.0.0.1:4317/api/research/personal-memory/rebuild-index", {
    method: "POST",
    headers: {
      Host: "127.0.0.1:4317",
      Origin: options.origin ?? "http://127.0.0.1:4317",
      "Content-Type": "application/json",
      "X-Newsroom-Session": "forged-client-session",
      Authorization: "Bearer forged-client-token",
    },
    body: options.raw ? String(body) : JSON.stringify(body),
  })
}

function upstreamJson(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } })
}
