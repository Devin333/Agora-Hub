import { describe, expect, it } from "vitest"
import {
  canonicalResultIdentity,
  canonicalSourceUrl,
  materialAccessState,
  materialPaperId,
  materialSourceUrl,
  materialSaveState,
  sameResearchMaterial,
  sortMaterials,
  stableResultMaterial,
  type MaterialSaveReceipt,
} from "./model"
import type { ResearchMaterial } from "@/lib/research/workspace-items"
import type { ResearchResult } from "@/lib/research/conversation"
import type { historyState } from "@/lib/research/history"

const result: ResearchResult = {
  id: "arxiv:2601.01234v2",
  kind: "papers",
  title: "Grounded Paper",
  description: "A real source result",
  source: "arXiv",
  url: "https://arxiv.org/abs/2601.01234v2",
  href: "/papers/2601.01234",
}

function material(overrides: Partial<ResearchMaterial> = {}): ResearchMaterial {
  return {
    id: "paper:existing",
    groupId: null,
    kind: "paper",
    title: "Existing",
    url: result.url,
    referenceId: "2601.01234v2",
    readerHref: "/papers/2601.01234/read",
    notes: "",
    favorite: false,
    readingStatus: "unread",
    tags: [],
    createdAt: 1,
    updatedAt: 2,
    ...overrides,
  }
}

describe("paper agent library model", () => {
  it("uses a bounded SHA-256 history id and preserves the real source version", async () => {
    const saved = await stableResultMaterial(result)
    expect(saved.id).toMatch(/^paper:sha256:[0-9a-f]{64}$/)
    expect(saved.referenceId).toBe("2601.01234v2")
    expect(saved.readerHref).toBe("/papers/2601.01234/read")
    expect(materialPaperId(saved)).toBe("2601.01234")
    expect(saved).toMatchObject({ favorite: false, readingStatus: "unread", tags: [] })
  })

  it("does not collide for punctuation variants or long identities sharing a prefix", async () => {
    const punctuationA = await stableResultMaterial({ ...result, id: "a.b", url: "https://example.org/a.b", href: undefined })
    const punctuationB = await stableResultMaterial({ ...result, id: "a-b", url: "https://example.org/a-b", href: undefined })
    const prefix = "x".repeat(300)
    const longA = await stableResultMaterial({ ...result, url: `https://example.org/${prefix}a`, href: undefined })
    const longB = await stableResultMaterial({ ...result, url: `https://example.org/${prefix}b`, href: undefined })
    expect(punctuationA.id).not.toBe(punctuationB.id)
    expect(longA.id).not.toBe(longB.id)
    expect(longA.id.length).toBeLessThanOrEqual(128)
  })

  it("deduplicates the same canonical source but keeps different source versions distinct", async () => {
    const existing = material()
    expect(sameResearchMaterial(existing, { ...result, url: "https://arxiv.org/pdf/2601.01234v2.pdf" })).toBe(true)
    expect(sameResearchMaterial(existing, { ...result, url: "https://arxiv.org/abs/2601.01234v3" })).toBe(false)
    const updated = await stableResultMaterial({ ...result, title: "Updated title" }, existing)
    expect(updated.id).toBe(existing.id)
    expect(updated.createdAt).toBe(1)
    expect(updated.title).toBe("Updated title")
  })

  it("does not invent a reader document for an opaque online source", async () => {
    const opaque = await stableResultMaterial({ ...result, id: "provider-result", href: undefined, url: "https://example.org/paper" })
    expect(opaque.referenceId).toBe("provider-result")
    expect(opaque.readerHref).toBeUndefined()
    expect(materialPaperId(opaque)).toBeNull()
  })

  it("canonical identity is source based and never title based", () => {
    expect(canonicalResultIdentity(result)).toBe(canonicalResultIdentity({ ...result, title: "Unrelated title revision" }))
  })

  it("sorts the local library deterministically without mutating source records", () => {
    const source = [
      material({ id: "paper:c", title: "中文 C", createdAt: 2, updatedAt: 5 }),
      material({ id: "paper:b", title: "Beta", createdAt: 3, updatedAt: 4 }),
      material({ id: "paper:a", title: "alpha", createdAt: 1, updatedAt: 5 }),
    ]
    expect(sortMaterials(source, "updated-desc").map(item => item.id)).toEqual(["paper:c", "paper:a", "paper:b"])
    expect(sortMaterials(source, "created-desc").map(item => item.id)).toEqual(["paper:b", "paper:c", "paper:a"])
    expect(sortMaterials(source, "title-asc").map(item => item.id)).toEqual(["paper:a", "paper:b", "paper:c"])
    expect(source.map(item => item.id)).toEqual(["paper:c", "paper:b", "paper:a"])
  })

  it("keeps metadata persistence separate from a verified reading entry", () => {
    expect(materialAccessState(material({ readerHref: undefined }))).toBe("metadata-only")
    expect(materialAccessState(material({ readerHref: "/not-a-reader-route" }))).toBe("metadata-only")
    expect(materialAccessState(material())).toBe("reader-projection")
    expect(materialAccessState(material({ kind: "pdf" }))).toBe("local-pdf")
  })

  it("only exposes safe HTTP(S) material sources", () => {
    expect(materialSourceUrl(material({ url: "https://example.org/paper?q=1" }))).toBe("https://example.org/paper?q=1")
    expect(materialSourceUrl(material({ url: "javascript:alert(1)" }))).toBeNull()
    expect(materialSourceUrl(material({ url: "https://user:secret@example.org/paper" }))).toBeNull()
    expect(materialSourceUrl(material({ url: "" }))).toBeNull()
  })

  it("reports pending until a newer server revision contains the written material", () => {
    const written = material({ updatedAt: 10 })
    const receipt: MaterialSaveReceipt = { owner: "user-1", material: written, baseRevision: 4, generation: 8 }
    expect(materialSaveState(receipt, { owner: "user-1", ready: true, status: "saving", dirty: true, revision: 4, generation: 8 }, written, "user-1")).toBe("pending")
    expect(materialSaveState(receipt, { owner: "user-1", ready: true, status: "synced", dirty: false, revision: 5, generation: 8 }, written, "user-1")).toBe("confirmed")
    expect(materialSaveState(receipt, { owner: "user-1", ready: true, status: "synced", dirty: false, revision: 5, generation: 8 }, undefined, "user-1")).toBe("pending")
    expect(materialSaveState(receipt, { owner: "user-1", ready: true, status: "synced", dirty: false, revision: 6, generation: 9 }, material({ updatedAt: 20, favorite: true }), "user-1")).toBe("pending")
  })


  it("normalizes equivalent DOI URLs and sorts query identity by code point", () => {
    expect(canonicalSourceUrl("http://dx.doi.org/10.1000/ABC.2?utm_source=x")).toBe("https://doi.org/10.1000/abc.2")
    expect(canonicalSourceUrl("https://doi.org/10.1000/abc.2")).toBe("https://doi.org/10.1000/abc.2")
    expect(canonicalSourceUrl("https://example.org/p?ä=1&z=2&a=3")).toBe("https://example.org/p?a=3&z=2&%C3%A4=1")
  })

  it("never treats missing source URLs as the same material", () => {
    expect(sameResearchMaterial(material({ url: "" }), { ...result, url: "" })).toBe(false)
  })
  it("keeps a failed CAS operation visibly failed instead of claiming persistence", () => {
    const written = material({ updatedAt: 10 })
    const receipt: MaterialSaveReceipt = { owner: "user-1", material: written, baseRevision: 4, generation: 8 }
    expect(materialSaveState(receipt, { owner: "user-1", ready: true, status: "conflict", dirty: true, revision: 4, generation: 8 }, written, "user-1")).toBe("failed")
    expect(materialSaveState(receipt, { owner: "user-1", ready: true, status: "error", dirty: true, revision: 4, generation: 8 }, written, "user-1")).toBe("failed")
  })
})


const syncedHistory: Pick<ReturnType<typeof historyState>, "owner" | "ready" | "status" | "dirty" | "revision" | "generation"> = {
  owner: "user-1", ready: true, status: "synced", dirty: false, revision: 4, generation: 8,
}

describe("material persistence without an App receipt", () => {
  const imported = material({ kind: "pdf", id: "import-1", url: "", title: "Imported.pdf" })

  it.each([
    ["local", false, "pending"],
    ["loading", false, "pending"],
    ["saving", true, "pending"],
    ["saving", false, "pending"],
    ["synced", true, "pending"],
    ["error", true, "failed"],
    ["conflict", true, "failed"],
    ["error", false, "failed"],
    ["conflict", false, "failed"],
    ["synced", false, "confirmed"],
  ] as const)("reports %s dirty=%s as %s for an imported record", (status, dirty, expected) => {
    expect(materialSaveState(undefined, { ...syncedHistory, status, dirty }, imported, "user-1")).toBe(expected)
  })

  it("confirms a loaded server record without requiring a new revision or an invented receipt", () => {
    expect(materialSaveState(undefined, syncedHistory, material(), "user-1")).toBe("confirmed")
  })

  it("recovers from a failed import write only after a clean synced acknowledgement", () => {
    const states = [
      { ...syncedHistory, status: "error" as const, dirty: true },
      { ...syncedHistory, status: "saving" as const, dirty: true },
      { ...syncedHistory, revision: 5 },
    ]
    expect(states.map(state => materialSaveState(undefined, state, imported, "user-1"))).toEqual(["failed", "pending", "confirmed"])
  })

  it.each([null, undefined, "other-user"])("does not confirm a record for workspace owner %s", owner => {
    expect(materialSaveState(undefined, { ...syncedHistory, owner }, imported, "user-1")).toBe("pending")
  })

  it.each([null, undefined, "other-user"])("requires the current session owner %s to match", owner => {
    expect(materialSaveState(undefined, syncedHistory, imported, owner)).toBe("pending")
  })

  it("does not expose previous-owner failures or unready/missing records as persistence", () => {
    expect(materialSaveState(undefined, { ...syncedHistory, owner: "other-user", status: "error" }, imported, "user-1")).toBe("pending")
    expect(materialSaveState(undefined, { ...syncedHistory, ready: false }, imported, "user-1")).toBe("pending")
    expect(materialSaveState(undefined, syncedHistory, undefined, "user-1")).toBe("pending")
  })

  it("preserves every receipt revision, generation and snapshot prerequisite", () => {
    const written = material({ updatedAt: 10 })
    const receipt: MaterialSaveReceipt = { owner: "user-1", material: written, baseRevision: 4, generation: 8 }
    const history = { ...syncedHistory, revision: 5 }
    expect(materialSaveState(receipt, { ...history, revision: 4 }, written, "user-1")).toBe("pending")
    expect(materialSaveState(receipt, { ...history, generation: 7 }, written, "user-1")).toBe("pending")
    expect(materialSaveState(receipt, history, { ...written, updatedAt: 9 }, "user-1")).toBe("pending")
    expect(materialSaveState(receipt, history, { ...written, title: "Changed" }, "user-1")).toBe("pending")
    expect(materialSaveState({ ...receipt, owner: "other-user" }, history, written, "user-1")).toBe("pending")
    expect(materialSaveState(receipt, history, written, "user-1")).toBe("confirmed")
  })
})
