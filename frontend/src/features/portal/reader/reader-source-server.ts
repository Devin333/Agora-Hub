import { createHash } from "node:crypto"
import type { Paper } from "@/lib/papers/types"
import type { PaperDocumentResponse } from "@/lib/paper-reader/types"
import { compileArxivSourceHtml, sourceSvgDimensions } from "./reader-source-compiler"

type SourceEntry = { payload: PaperDocumentResponse; assets: Map<string, { bytes: Uint8Array; mime: string; checksum: string }>; expires: number; byteSize: number }
const sources = new Map<string, SourceEntry>()
const pending = new Map<string, Promise<SourceEntry | null>>()
const MAX_SOURCES = 8
const MAX_SOURCE_BYTES = 16 * 1024 * 1024
const MAX_CACHE_BYTES = 48 * 1024 * 1024
const TTL = 30 * 60 * 1000

/** Readiness reflects an existing, verified full-text artifact; never compile during search. */
export function cachedReaderReady(paperId: string): boolean {
  return Array.from(sources.values()).some(entry => entry.expires > Date.now()
    && entry.payload.document?.paperId === paperId
    && entry.payload.document.status === "compiled"
    && entry.payload.status.status === "compiled"
    && entry.payload.status.gateReport?.passed === true)
}

export function arxivSourceHtmlUrl(paper: Paper): string | undefined {
  for (const value of [paper.arxivUrl, paper.paperUrl, paper.pdfUrl]) {
    if (!value) continue
    try {
      const url = new URL(value)
      if (!["arxiv.org", "www.arxiv.org"].includes(url.hostname)) continue
      const match = /^\/(?:abs|pdf|html)\/(\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?$/.exec(url.pathname)
      if (match) return `https://arxiv.org/html/${match[1]}`
    } catch { continue }
  }
}

export async function loadReaderSource(paper: Paper): Promise<SourceEntry | null> {
  const sourceUrl = arxivSourceHtmlUrl(paper)
  if (!sourceUrl) return null
  for (const [cachedKey, cached] of sources) if (cached.expires <= Date.now()) sources.delete(cachedKey)
  const key = `${paper.id}:${sourceUrl}:${paper.title}`
  const existing = sources.get(key)
  if (existing && existing.expires > Date.now()) return existing
  const inFlight = pending.get(key)
  if (inFlight) return inFlight
  if (pending.size >= 4) return null
  const operation = compileSource(paper, sourceUrl).then((result) => {
    if (result) {
      sources.delete(key)
      sources.set(key, result)
      while (sources.size > MAX_SOURCES || Array.from(sources.values()).reduce((size, entry) => size + entry.byteSize, 0) > MAX_CACHE_BYTES) sources.delete(sources.keys().next().value!)
    }
    return result
  }).finally(() => pending.delete(key))
  pending.set(key, operation)
  return operation
}

async function compileSource(paper: Paper, sourceUrl: string): Promise<SourceEntry | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 18000)
  try {
    const response = await fetch(sourceUrl, { signal: controller.signal, redirect: "manual", cache: "no-store" })
    if (!response.ok || !response.headers.get("content-type")?.includes("text/html")) return null
    const bytes = await readBounded(response, 4 * 1024 * 1024)
    const sourceHash = createHash("sha256").update(bytes).digest("hex")
    const payload = compileArxivSourceHtml(paper, new TextDecoder().decode(bytes), sourceUrl, sourceHash)
    if (!payload?.document || !payload.manifest) return null
    const assets: SourceEntry["assets"] = new Map()
    let byteSize = bytes.length
    if (payload.manifest.assets.length > 48) return null
    // Fetch only compiler-enumerated source images, with bounded concurrency and size.
    for (let offset = 0; offset < payload.manifest.assets.length; offset += 4) {
      await Promise.all(payload.manifest.assets.slice(offset, offset + 4).map(async (asset) => {
        const assetUrl = asset.metadata?.sourceUrl
        if (typeof assetUrl !== "string" || !isArxivSourceAssetUrl(assetUrl, sourceUrl)) throw new Error("invalid_source_asset")
        const image = await fetch(assetUrl, { signal: controller.signal, redirect: "error", cache: "no-store" })
        if (!image.ok) throw new Error("source_asset_unavailable")
        const content = await readBounded(image, 3 * 1024 * 1024)
        byteSize += content.length
        if (byteSize > MAX_SOURCE_BYTES) throw new Error("source_assets_too_large")
        const mime = image.headers.get("content-type")?.split(";")[0] || ""
        const dimensions = sourceImageDimensions(content, mime)
        if (!dimensions) throw new Error("unsupported_source_asset")
        asset.width = dimensions.width
        asset.height = dimensions.height
        asset.checksum = createHash("sha256").update(content).digest("hex")
        asset.fileSize = content.length
        asset.mimeType = mime
        asset.metadata = { ...asset.metadata, publicUrl: `/api/papers/${encodeURIComponent(paper.id)}/source-assets/${asset.assetId}?v=${asset.checksum}` }
        assets.set(asset.assetId, { bytes: content, mime, checksum: asset.checksum })
      }))
    }
    payload.document.status = "compiled"
    payload.status.status = "compiled"
    payload.status.gateReport = { ...payload.status.gateReport, passed: true, verifiedAssets: assets.size }
    return { payload, assets, expires: Date.now() + TTL, byteSize }
  } catch { return null }
  finally { controller.abort(); clearTimeout(timer) }
}

function sourceImageDimensions(bytes: Uint8Array, mime: string): { width: number; height: number } | null {
  if (mime === "image/svg+xml") return sourceSvgDimensions(new TextDecoder().decode(bytes))
  const buffer = Buffer.from(bytes)
  if (mime !== "image/png" || buffer.length < 24 || buffer.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a" || buffer.toString("ascii", 12, 16) !== "IHDR") return null
  const width = buffer.readUInt32BE(16), height = buffer.readUInt32BE(20)
  return width > 0 && height > 0 && width * height <= 40000000 ? { width, height } : null
}

export function isArxivSourceAssetUrl(value: string, sourceUrl: string): boolean {
  try {
    const url = new URL(value)
    const paperPath = new URL(sourceUrl).pathname.replace(/^\/html\//, "")
    const expectedPath = paperPath.replace(".", "\\.") + (/v\d+$/.test(paperPath) ? "" : "(?:v\\d+)?")
    return url.origin === "https://arxiv.org" && !url.username && !url.password && !url.search
      && new RegExp(`^/html/${expectedPath}/`).test(url.pathname)
      && !decodeURIComponent(url.pathname).split("/").includes("..")
  } catch { return false }
}

async function readBounded(response: Response, limit: number): Promise<Uint8Array> {
  if (Number(response.headers.get("content-length") || 0) > limit || !response.body) throw new Error("source_too_large")
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let length = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      length += value.length
      if (length > limit) throw new Error("source_too_large")
      chunks.push(value)
    }
    return Buffer.concat(chunks, length)
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
}
