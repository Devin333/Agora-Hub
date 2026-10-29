import { NextRequest } from "next/server"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { sanitizePersonalMemoryUpdate } from "@/lib/research/personal-memory-contracts"

const state = vi.hoisted(() => ({ token: "session-token" as string | undefined }))
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => state.token ? { value: state.token } : undefined }),
  headers: async () => new Headers(),
}))

import { personalMemoryPatch } from "./_server"

describe("personal memory BFF server boundary", () => {
  const originalProfile = process.env.NEWSROOM_DEPLOYMENT_PROFILE
  const originalOrigin = process.env.NEWSROOM_PUBLIC_ORIGIN
  const originalInsecure = process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN
  const originalApi = process.env.NEWSROOM_API_BASE_URL

  beforeEach(() => {
    state.token = "session-token"
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    process.env.NEWSROOM_PUBLIC_ORIGIN = "http://127.0.0.1:4317"
    process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN = "1"
    process.env.NEWSROOM_API_BASE_URL = "http://127.0.0.1:4318"
  })
  afterEach(() => {
    restore("NEWSROOM_DEPLOYMENT_PROFILE", originalProfile)
    restore("NEWSROOM_PUBLIC_ORIGIN", originalOrigin)
    restore("NEWSROOM_ALLOW_INSECURE_ORIGIN", originalInsecure)
    restore("NEWSROOM_API_BASE_URL", originalApi)
    vi.unstubAllGlobals()
  })

  it("forwards validated CAS payload with server session and public origin", async () => {
    const backend = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      success: true,
      data: { record: { memory_id: `pmem_${"a".repeat(32)}`, revision: 2 } },
    }), { headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", backend)
    const request = mutationRequest({ revision: 1, content: "Updated preference" })
    const response = await personalMemoryPatch(
      "/api/v1/research/desktop/memory/pmem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      request,
      sanitizePersonalMemoryUpdate,
    )
    expect(response.status).toBe(200)
    expect(response.headers.get("cache-control")).toBe("private, no-store")
    expect(await response.json()).toEqual({
      success: true,
      data: { record: { memory_id: `pmem_${"a".repeat(32)}`, revision: 2 } },
    })
    const [url, init] = backend.mock.calls[0] as [string, RequestInit]
    expect(url).toBe("http://127.0.0.1:4318/api/v1/research/desktop/memory/pmem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    expect(init.method).toBe("PATCH")
    const headers = new Headers(init.headers)
    expect(headers.get("X-Newsroom-Session")).toBe("session-token")
    expect(headers.get("Origin")).toBe("http://127.0.0.1:4317")
    expect(JSON.parse(String(init.body))).toEqual({ revision: 1, content: "Updated preference" })
  })

  it("rejects cross-origin, owner fields, and missing private session before forwarding", async () => {
    const backend = vi.fn()
    vi.stubGlobal("fetch", backend)
    const crossOrigin = await personalMemoryPatch(
      "/api/v1/research/desktop/memory/pmem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      new NextRequest("http://127.0.0.1:4317/api/research/personal-memory/x", {
        method: "PATCH",
        headers: { Origin: "http://evil.invalid", Host: "127.0.0.1:4317", "Content-Type": "application/json" },
        body: JSON.stringify({ revision: 1, content: "Updated" }),
      }),
      sanitizePersonalMemoryUpdate,
    )
    expect(crossOrigin.status).toBe(403)
    const forged = await personalMemoryPatch(
      "/api/v1/research/desktop/memory/pmem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      mutationRequest({ revision: 1, content: "Updated", user_id: "forged" }),
      sanitizePersonalMemoryUpdate,
    )
    expect(forged.status).toBe(400)
    state.token = undefined
    const anonymous = await personalMemoryPatch(
      "/api/v1/research/desktop/memory/pmem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      mutationRequest({ revision: 1, content: "Updated" }),
      sanitizePersonalMemoryUpdate,
    )
    expect(anonymous.status).toBe(401)
    expect(backend).not.toHaveBeenCalled()
  })
})

function mutationRequest(body: Record<string, unknown>) {
  return new NextRequest("http://127.0.0.1:4317/api/research/personal-memory/x", {
    method: "PATCH",
    headers: {
      Origin: "http://127.0.0.1:4317",
      Host: "127.0.0.1:4317",
      "Sec-Fetch-Site": "same-origin",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  })
}

function restore(name: string, value: string | undefined) {
  if (value === undefined) delete process.env[name]
  else process.env[name] = value
}
