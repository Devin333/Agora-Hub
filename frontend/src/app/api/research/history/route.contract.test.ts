import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"

vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => ({ value: "session-token" }) }),
  headers: async () => new Headers(),
}))

import { PUT } from "@/app/api/research/history/route"

const envKeys = [
  "NEWSROOM_DEPLOYMENT_PROFILE",
  "NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE",
  "NEWSROOM_PUBLIC_ORIGIN",
  "NEWSROOM_ALLOW_INSECURE_ORIGIN",
  "NEWSROOM_API_BASE_URL",
] as const
const originalEnv = Object.fromEntries(envKeys.map(key => [key, process.env[key]]))

describe("research history upstream contract", () => {
  beforeEach(() => {
    process.env.NEWSROOM_API_BASE_URL = "http://127.0.0.1:8120"
    delete process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    for (const key of envKeys) {
      const value = originalEnv[key]
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  })

  it("forwards the configured public Origin to the hosted upstream", async () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    process.env.NEWSROOM_PUBLIC_ORIGIN = "http://127.0.0.1:3120"
    process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN = "1"
    const upstream = successfulUpstream()
    vi.stubGlobal("fetch", upstream)

    const response = await PUT(historyRequest({
      url: "http://localhost:3120/api/research/history",
      host: "127.0.0.1:3120",
      origin: "http://127.0.0.1:3120",
    }))

    expect(response.status).toBe(200)
    expect(upstream).toHaveBeenCalledOnce()
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit]
    expect(url).toBe("http://127.0.0.1:8120/api/v1/research/history")
    expect(init.method).toBe("PUT")
    const headers = new Headers(init.headers)
    expect(headers.get("Origin")).toBe("http://127.0.0.1:3120")
    expect(headers.get("x-newsroom-session")).toBe("session-token")
    expect(init.cache).toBe("no-store")
  })

  it("rejects a foreign browser Origin before making an upstream call", async () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    process.env.NEWSROOM_PUBLIC_ORIGIN = "http://127.0.0.1:3120"
    process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN = "1"
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await PUT(historyRequest({
      url: "http://localhost:3120/api/research/history",
      host: "127.0.0.1:3120",
      origin: "https://foreign.example",
      site: "cross-site",
    }))

    expect(response.status).toBe(403)
    expect(upstream).not.toHaveBeenCalled()
  })

  it("keeps default-profile forwarding unchanged without a synthetic Origin", async () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    delete process.env.NEWSROOM_PUBLIC_ORIGIN
    const upstream = successfulUpstream()
    vi.stubGlobal("fetch", upstream)

    const response = await PUT(historyRequest({
      url: "http://localhost:3000/api/research/history",
      host: "localhost:3000",
      origin: "http://localhost:3000",
    }))

    expect(response.status).toBe(200)
    const [, init] = upstream.mock.calls[0] as [string, RequestInit]
    const headers = new Headers(init.headers)
    expect(headers.has("Origin")).toBe(false)
    expect(headers.get("x-newsroom-session")).toBe("session-token")
  })
})

function historyRequest({ url, host, origin, site = "same-origin" }: { url: string; host: string; origin: string; site?: string }) {
  return new NextRequest(url, {
    method: "PUT",
    headers: {
      Host: host,
      Origin: origin,
      "Sec-Fetch-Site": site,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ revision: 1, visits: [], groups: [] }),
  })
}

function successfulUpstream() {
  return vi.fn().mockResolvedValue(new Response(JSON.stringify({
    success: true,
    data: { revision: 2, visits: [], groups: [] },
  }), { headers: { "Content-Type": "application/json" } }))
}
