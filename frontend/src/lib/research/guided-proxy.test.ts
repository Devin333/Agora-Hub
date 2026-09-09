import { beforeEach, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { cachedReaderReady } from "@/features/portal/reader/reader-source-server"
import { guidedResearchProxy } from "./guided-proxy"

vi.mock("@/lib/api/server", () => ({ safeApiPost: vi.fn() }))
vi.mock("@/features/portal/reader/reader-source-server", () => ({ cachedReaderReady: vi.fn() }))
vi.mock("next/headers", () => ({ cookies: () => ({ get: () => ({ value: "owned-session" }) }) }))
const origin = "http://localhost:3000"
const post = (body: unknown, requestOrigin = origin) => new NextRequest(`${origin}/api/research/search`, { method: "POST", headers: { Origin: requestOrigin }, body: JSON.stringify(body) })
const result = { source: "papers", results: [{ id: "paper-1", kind: "papers", title: "Paper", description: "Source-backed abstract", source: "arXiv", url: "https://arxiv.org/abs/2605.22343", href: "/design-demo/papers/paper-1/read" }], total: 1, moreHref: "/design-demo/papers?q=agent&has=code" }
beforeEach(() => { vi.resetAllMocks() })

it("rejects foreign origin and oversized bodies without contacting the service", async () => {
  expect((await guidedResearchProxy(post({}, "https://foreign.test"), "search")).status).toBe(403)
  expect((await guidedResearchProxy(post({ question: "x".repeat(256 * 1024) }), "search")).status).toBe(413)
  expect(safeApiPost).not.toHaveBeenCalled()
})

it("forwards owned session context but only offers a reader for verified full text", async () => {
  vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: result })
  vi.mocked(cachedReaderReady).mockReturnValue(false)
  const response = await guidedResearchProxy(post({ source: "papers" }), "search")
  const payload = await response.json()
  expect(payload.data.results[0].href).toBeUndefined()
  expect(payload.data.results[0].url).toBe(result.results[0].url)
  expect(payload.data.moreHref).toBe(result.moreHref)
  expect(safeApiPost).toHaveBeenCalledWith("/api/v1/research/guided/search", { source: "papers" }, expect.objectContaining({ headers: { "x-newsroom-session": "owned-session" }, signal: expect.any(AbortSignal) }))
  expect(response.headers.get("cache-control")).toBe("no-store")
  vi.mocked(cachedReaderReady).mockReturnValue(true)
  expect((await (await guidedResearchProxy(post({ source: "papers" }), "search")).json()).data.results[0].href).toBe("/design-demo/papers/paper-1/read")
})

it("fails closed on malformed or unsafe service results", async () => {
  vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { ...result, results: [{ ...result.results[0], url: "javascript:alert(1)" }] } })
  expect((await guidedResearchProxy(post({}), "search")).status).toBe(502)
})

it("accepts filtered project results without an unsupported continuation link", async () => {
  vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { source: "projects", results: [], total: 0 } })
  const response = await guidedResearchProxy(post({ source: "projects" }), "search")
  expect(response.status).toBe(200)
  expect((await response.json()).data.moreHref).toBeUndefined()
})
