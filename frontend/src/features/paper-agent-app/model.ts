import type { ResearchMaterial } from "@/lib/research/workspace-items"
import type { ResearchResult } from "@/lib/research/conversation"
import type { historyState } from "@/lib/research/history"

export type LibraryFilter = "all" | "unread" | "saved"
export type LibrarySort = "updated-desc" | "created-desc" | "title-asc"
export type MaterialAccessState = "metadata-only" | "reader-projection" | "local-pdf"
export type ReaderTab = "original" | "overview" | "notes" | "details"
export type ReadingStatus = "unread" | "reading" | "read"

export type PaperAgentMaterial = ResearchMaterial


export type MaterialSaveReceipt = {
  owner: string
  material: PaperAgentMaterial
  baseRevision: number
  generation: number
}

export type MaterialSaveState = "pending" | "confirmed" | "failed"
type MaterialSaveHistory = Pick<ReturnType<typeof historyState>, "owner" | "ready" | "status" | "dirty" | "revision" | "generation">

export function materialPaperId(material: ResearchMaterial): string | null {
  if (!material.readerHref) return null
  try {
    const url = new URL(material.readerHref, "https://agora.invalid")
    const match = url.pathname.match(/^\/papers\/([^/]+)\/read$/)
    return match ? decodeURIComponent(match[1]) : null
  } catch {
    return null
  }
}

export function materialAccessState(material: PaperAgentMaterial): MaterialAccessState {
  if (!materialPaperId(material)) return "metadata-only"
  return material.kind === "pdf" ? "local-pdf" : "reader-projection"
}

export function materialSourceUrl(material: PaperAgentMaterial): string | null {
  const value = material.url.trim()
  if (!value) return null
  try {
    const url = new URL(value)
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password ? url.toString() : null
  } catch {
    return null
  }
}

export function sortMaterials(materials: Iterable<PaperAgentMaterial>, sort: LibrarySort): PaperAgentMaterial[] {
  return [...materials].sort((left, right) => {
    if (sort === "updated-desc") {
      return right.updatedAt - left.updatedAt || right.createdAt - left.createdAt || compareMaterialIdentity(left, right)
    }
    if (sort === "created-desc") {
      return right.createdAt - left.createdAt || right.updatedAt - left.updatedAt || compareMaterialIdentity(left, right)
    }
    return compareMaterialIdentity(left, right)
  })
}

export function materialFavorite(material: PaperAgentMaterial): boolean {
  return material.favorite === true
}

export function materialReadingStatus(material: PaperAgentMaterial): ReadingStatus {
  return material.readingStatus === "reading" || material.readingStatus === "read" ? material.readingStatus : "unread"
}

export function materialTags(material: PaperAgentMaterial): string[] {
  return normalizeTags(material.tags ?? [])
}

export function normalizeTags(values: Iterable<string>): string[] {
  const result: string[] = []
  const seen = new Set<string>()
  for (const raw of values) {
    const value = raw.trim().replace(/\s+/g, " ").slice(0, 64)
    const key = value.toLocaleLowerCase()
    if (!value || seen.has(key)) continue
    seen.add(key)
    result.push(value)
    if (result.length === 32) break
  }
  return result
}

export function parseTags(value: string): string[] {
  return normalizeTags(value.split(/[,，\n]/))
}

export function patchMaterial(material: PaperAgentMaterial, patch: Partial<Pick<PaperAgentMaterial, "favorite" | "readingStatus" | "tags" | "groupId">>): PaperAgentMaterial {
  return { ...material, ...patch, ...(patch.tags ? { tags: normalizeTags(patch.tags) } : {}), updatedAt: Date.now() }
}

/**
 * A material id is a SHA-256 of the canonical source identity. It is bounded for
 * History ids, collision resistant, and never stores an unbounded source URL.
 */
export async function stableResultMaterial(result: ResearchResult, existing?: PaperAgentMaterial): Promise<PaperAgentMaterial> {
  const now = Date.now()
  const referenceId = resultReferenceId(result)
  const readerId = resultReaderId(result)
  const stableId = existing?.id ?? `paper:sha256:${await sha256Hex(canonicalResultIdentity(result))}`
  return {
    id: stableId,
    groupId: existing?.groupId ?? null,
    kind: "paper",
    title: result.title,
    url: result.url,
    ...(referenceId ? { referenceId } : {}),
    ...(readerId ? { readerHref: `/papers/${encodeURIComponent(readerId)}/read` } : {}),
    notes: result.description,
    favorite: existing?.favorite ?? false,
    readingStatus: existing?.readingStatus ?? "unread",
    tags: normalizeTags(existing?.tags ?? []),
    createdAt: existing?.createdAt ?? now,
    updatedAt: now,
  } as PaperAgentMaterial
}

export function sameResearchMaterial(material: ResearchMaterial, result: ResearchResult): boolean {
  if (!material.url.trim() || !result.url.trim()) return false
  return canonicalSourceUrl(material.url) === canonicalSourceUrl(result.url)
}

/** Local presence is not persistence: imports and loaded records also need an owned, clean server acknowledgement. */
export function materialSaveState(
  receipt: MaterialSaveReceipt | undefined,
  history: MaterialSaveHistory,
  current: ResearchMaterial | undefined,
  expectedOwner: string | null | undefined,
): MaterialSaveState {
  if (!expectedOwner || history.owner !== expectedOwner || !history.ready || (receipt && receipt.owner !== expectedOwner)) return "pending"
  if (history.status === "error" || history.status === "conflict") return "failed"
  if (history.status !== "synced" || history.dirty || current === undefined) return "pending"
  if (!receipt) return "confirmed"
  if (
    history.revision > receipt.baseRevision &&
    history.generation >= receipt.generation &&
    current.updatedAt >= receipt.material.updatedAt &&
    sameMaterialSnapshot(current, receipt.material)
  ) return "confirmed"
  return "pending"
}

export function canonicalResultIdentity(result: ResearchResult): string {
  return `${result.kind}\n${canonicalSourceUrl(result.url)}`
}

export function canonicalSourceUrl(value: string): string {
  try {
    const url = new URL(value)
    url.hash = ""
    url.hostname = url.hostname.toLocaleLowerCase().replace(/^www\./, "")
    if ((url.protocol === "https:" && url.port === "443") || (url.protocol === "http:" && url.port === "80")) url.port = ""
    const arxiv = arxivIdentity(url)
    if (arxiv) return `https://arxiv.org/abs/${arxiv}`
    const doi = doiIdentity(url)
    if (doi) return `https://doi.org/${doi}`
    const sorted = [...url.searchParams.entries()].sort(([aKey, aValue], [bKey, bValue]) => codePointCompare(aKey, bKey) || codePointCompare(aValue, bValue))
    url.search = ""
    for (const [key, item] of sorted) url.searchParams.append(key, item)
    if (url.pathname !== "/") url.pathname = url.pathname.replace(/\/+$/, "")
    return url.toString()
  } catch {
    return value.trim()
  }
}

function resultReferenceId(result: ResearchResult): string {
  try {
    const arxiv = arxivIdentity(new URL(result.url))
    if (arxiv) return arxiv
  } catch { /* guided results have already passed URL validation */ }
  return resultReaderId(result) ?? result.id
}

function resultReaderId(result: ResearchResult): string | null {
  if (!result.href) return null
  try {
    const match = new URL(result.href, "https://agora.invalid").pathname.match(/^\/papers\/([^/]+)$/)
    return match ? decodeURIComponent(match[1]) : null
  } catch {
    return null
  }
}

function arxivIdentity(url: URL): string | null {
  if (url.hostname.toLocaleLowerCase().replace(/^www\./, "") !== "arxiv.org") return null
  const match = url.pathname.match(/^\/(?:abs|pdf)\/([^/]+?)(?:\.pdf)?\/?$/i)
  return match ? decodeURIComponent(match[1]) : null
}

function doiIdentity(url: URL): string | null {
  const host = url.hostname.toLocaleLowerCase().replace(/^www\./, "")
  if (host !== "doi.org" && host !== "dx.doi.org") return null
  const value = decodeURIComponent(url.pathname.replace(/^\/+/, "")).trim()
  return /^10\.\d{4,9}\/.+$/i.test(value) ? value.toLocaleLowerCase() : null
}

function compareMaterialIdentity(left: PaperAgentMaterial, right: PaperAgentMaterial): number {
  const leftTitle = left.title.trim().toLocaleLowerCase()
  const rightTitle = right.title.trim().toLocaleLowerCase()
  return codePointCompare(leftTitle, rightTitle) || codePointCompare(left.title, right.title) || codePointCompare(left.id, right.id)
}

function codePointCompare(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0
}

function sameMaterialSnapshot(current: ResearchMaterial, expected: ResearchMaterial): boolean {
  return current.id === expected.id &&
    current.groupId === expected.groupId &&
    current.kind === expected.kind &&
    current.title === expected.title &&
    current.url === expected.url &&
    current.referenceId === expected.referenceId &&
    current.readerHref === expected.readerHref &&
    current.notes === expected.notes &&
    materialFavorite(current) === materialFavorite(expected) &&
    materialReadingStatus(current) === materialReadingStatus(expected) &&
    materialTags(current).length === materialTags(expected).length &&
    materialTags(current).every((tag, index) => tag === materialTags(expected)[index])
}
async function sha256Hex(value: string): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error("当前浏览器不支持安全的 SHA-256 论文身份生成。")
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("")
}
