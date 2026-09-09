import { createHash } from "node:crypto"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { sourceHtml, sourcePaper } from "./source-fixture"

// Real encoded one-pixel PNG fixture; production only fetches publisher source assets.
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64")

describe("source acquisition and asset publication", () => {
  beforeEach(() => { vi.resetModules(); vi.stubGlobal("fetch", vi.fn()) })
  afterEach(() => vi.unstubAllGlobals())

  it("publishes only complete verified assets and reuses the bounded source cache", async () => {
    const { loadReaderSource, cachedReaderReady } = await import("../reader-source-server")
    expect(cachedReaderReady(sourcePaper.id)).toBe(false)
    vi.mocked(fetch).mockImplementation(async (url) => String(url).endsWith(".png")
      ? new Response(png, { headers: { "content-type": "image/png" } })
      : new Response(sourceHtml(true), { headers: { "content-type": "text/html" } }))
    const result = await loadReaderSource(sourcePaper)
    expect(cachedReaderReady(sourcePaper.id)).toBe(true)
    expect(result?.payload.status).toMatchObject({ status: "compiled", gateReport: { passed: true, verifiedAssets: 2 } })
    expect(result?.payload.manifest?.assets[0]).toMatchObject({ width: 1, height: 1, checksum: createHash("sha256").update(png).digest("hex"), fileSize: png.length })
    expect(result?.payload.manifest?.assets[0].metadata?.publicUrl).toContain("?v=")
    expect(await loadReaderSource(sourcePaper)).toBe(result)
    expect(fetch).toHaveBeenCalledTimes(3)
  })

  it("withholds the full document if any required image is missing or invalid", async () => {
    const { loadReaderSource } = await import("../reader-source-server")
    vi.mocked(fetch).mockImplementation(async (url) => String(url).endsWith(".png")
      ? new Response("not an image", { headers: { "content-type": "image/png" } })
      : new Response(sourceHtml(true), { headers: { "content-type": "text/html" } }))
    expect(await loadReaderSource(sourcePaper)).toBeNull()
    vi.mocked(fetch).mockResolvedValue(new Response("", { status: 302, headers: { location: "http://127.0.0.1/private" } }))
    expect(await loadReaderSource(sourcePaper)).toBeNull()
  })

  it("rejects oversized sources before reading the body", async () => {
    const { loadReaderSource } = await import("../reader-source-server")
    vi.mocked(fetch).mockResolvedValue(new Response(sourceHtml(), { headers: { "content-type": "text/html", "content-length": "5000000" } }))
    expect(await loadReaderSource(sourcePaper)).toBeNull()
  })

  it("limits sources to public arXiv identities and same-paper, same-version image paths", async () => {
    const { arxivSourceHtmlUrl, isArxivSourceAssetUrl } = await import("../reader-source-server")
    const source = "https://arxiv.org/html/2605.22343v1"
    expect(arxivSourceHtmlUrl(sourcePaper)).toBe(source)
    expect(arxivSourceHtmlUrl({ ...sourcePaper, arxivUrl: "https://example.com/abs/2605.22343" })).toBeUndefined()
    expect(isArxivSourceAssetUrl(`${source}/image.png`, source)).toBe(true)
    for (const value of ["http://arxiv.org/html/2605.22343v1/x.png", "https://arxiv.org.evil.test/html/2605.22343v1/x.png", "https://arxiv.org/html/2605.22343v2/x.png", "https://arxiv.org/html/2605.00000v1/x.png", `${source}/../private`, `${source}/%2e%2e/private`, `${source}/image.png?redirect=private`]) expect(isArxivSourceAssetUrl(value, source)).toBe(false)
  })
})
