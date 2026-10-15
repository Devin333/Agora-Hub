import { afterEach, describe, expect, it, vi } from "vitest"
import {
  createPersonalMemory,
  deletePersonalMemory,
  exportPersonalMemory,
  getPersonalMemoryPolicy,
  listPersonalMemoryPage,
  rebuildPersonalMemoryIndex,
  rejectPersonalMemoryCandidate,
  setPersonalMemoryStatus,
  updatePersonalMemoryPolicy,
} from "./personal-memory-client"

const sourceRef = { source_type: "paper" as const, source_ref: "2601.01234", source_revision: "sha256:source", locator: "page:2", label: "Grounded Paper" }
const record = { memory_id: `pmem_${"1".repeat(32)}`, content: "Prefer reproducible evaluations", category: "reading_preference" as const, source_refs: [sourceRef], status: "active" as const, revision: 3, origin: "user_confirmed" as const, confirmed_at: "2026-10-14T00:00:00Z", disabled_at: null, deleted_at: null, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" }
const policy = { allow_remote_processing: false, remote_processing_permissions: { model: false, tool: false, embedding: false }, revision: 2, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" }
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } })
afterEach(() => vi.unstubAllGlobals())

describe("personal memory client", () => {
  it("unwraps the authenticated BFF envelope and parses structured provenance", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ success: true, data: { records: [record], total: 1, next_cursor: null } })); vi.stubGlobal("fetch", fetch)
    await expect(listPersonalMemoryPage()).resolves.toMatchObject({ records: [record], total: 1, nextCursor: null })
    expect(fetch).toHaveBeenCalledWith("/api/research/personal-memory?limit=50", expect.objectContaining({ credentials: "same-origin", cache: "no-store" }))
  })


  it("retains total and cursor so records beyond the first 50 remain manageable", async () => {
    const pageOne = Array.from({ length: 50 }, (_, index) => ({ ...record, memory_id: `pmem_${index.toString(16).padStart(32, "0")}` }))
    const pageTwo = [{ ...record, memory_id: `pmem_${"f".repeat(32)}`, status: "disabled" as const }]
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ records: pageOne, total: 51, next_cursor: "50" }))
      .mockResolvedValueOnce(json({ records: pageTwo, total: 51, next_cursor: null }))
    vi.stubGlobal("fetch", fetch)
    const first = await listPersonalMemoryPage({ limit: 50 })
    expect(first).toMatchObject({ total: 51, nextCursor: "50" })
    const second = await listPersonalMemoryPage({ limit: 50, cursor: first.nextCursor ?? undefined, statuses: ["candidate", "active", "disabled"] })
    expect(second.records[0]).toMatchObject({ memory_id: pageTwo[0].memory_id, status: "disabled" })
    expect(fetch.mock.calls[1][0]).toContain("cursor=50")
    expect(fetch.mock.calls[1][0]).toContain("status=disabled")
  })
  it("creates a manually confirmed record with idempotent request identity and real refs", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ success: true, data: { record } })); vi.stubGlobal("fetch", fetch)
    await createPersonalMemory({ content: record.content, category: record.category, source_refs: [sourceRef], creation: "confirmed", request_id: "paper-agent/request-1" })
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ content: record.content, category: record.category, source_refs: [sourceRef], creation: "confirmed", request_id: "paper-agent/request-1" })
  })

  it("uses explicit reject, status, and delete operations with revision CAS bodies", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ success: true, data: { record: { ...record, status: "deleted", revision: 4 } } }))
      .mockResolvedValueOnce(json({ success: true, data: { record: { ...record, status: "disabled", revision: 4 } } }))
      .mockResolvedValueOnce(json({ success: true, data: { record: { ...record, status: "deleted", revision: 5 } } }))
    vi.stubGlobal("fetch", fetch)
    await rejectPersonalMemoryCandidate(record.memory_id, record.revision)
    expect(fetch.mock.calls[0][0]).toBe(`/api/research/personal-memory/${record.memory_id}`)
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ revision: 3 })
    await setPersonalMemoryStatus(record.memory_id, record.revision, "disabled")
    expect(fetch.mock.calls[1][0]).toBe(`/api/research/personal-memory/${record.memory_id}/status`)
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ status: "disabled", revision: 3 })
    await deletePersonalMemory(record.memory_id, 4)
    expect(fetch.mock.calls[2][0]).toBe(`/api/research/personal-memory/${record.memory_id}`)
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ revision: 4 })
  })

  it("loads and saves the authoritative remote-processing policy with revision", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ policy })).mockResolvedValueOnce(json({ policy: { ...policy, allow_remote_processing: true, remote_processing_permissions: { model: true, tool: true, embedding: true }, revision: 3 } })); vi.stubGlobal("fetch", fetch)
    await expect(getPersonalMemoryPolicy()).resolves.toMatchObject({
      allow_remote_processing: false,
      remote_processing_permissions: { model: false, tool: false, embedding: false },
      revision: 2,
    })
    await updatePersonalMemoryPolicy(2, true)
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ revision: 2, allow_remote_processing: true })
  })

  it("requests a deterministic local recall-index rebuild", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ indexed_records: 7 }))
    vi.stubGlobal("fetch", fetch)
    await expect(rebuildPersonalMemoryIndex()).resolves.toEqual({ indexedRecords: 7 })
    expect(fetch).toHaveBeenCalledWith(
      "/api/research/personal-memory/rebuild-index",
      expect.objectContaining({ method: "POST", credentials: "same-origin", cache: "no-store", body: "{}" }),
    )
  })

  it("explicitly excludes deleted records from the default export", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ success: true, data: { export: { schema_version: "1", exported_at: "2026-10-14T00:00:00Z", records: [record], policy, notices: [] } } })); vi.stubGlobal("fetch", fetch)
    const exported = await exportPersonalMemory()
    expect(fetch.mock.calls[0][0]).toBe("/api/research/personal-memory/export?include_deleted=false")
    await expect(exported.blob.text()).resolves.toContain(record.memory_id)
  })

  it("does not retain provider configuration or owner metadata in renderer records and exports", async () => {
    const unsafeRecord = { ...record, api_key: "provider-secret", owner: { user_id: "private-user" }, provider: { token: "provider-token" } }
    const unsafePolicy = { ...policy, api_key: "policy-secret" }
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ records: [unsafeRecord], total: 1, next_cursor: null }))
      .mockResolvedValueOnce(json({ export: { schema_version: "1", exported_at: "2026-10-15T00:00:00Z", records: [unsafeRecord], policy: unsafePolicy, notices: [] } }))
    vi.stubGlobal("fetch", fetch)
    const page = await listPersonalMemoryPage()
    expect(page.records[0]).toEqual(record)
    const exported = await exportPersonalMemory()
    const serialized = await exported.blob.text()
    expect(serialized).not.toContain("provider-secret")
    expect(serialized).not.toContain("provider-token")
    expect(serialized).not.toContain("policy-secret")
    expect(serialized).not.toContain("private-user")
    expect(serialized).toContain(record.memory_id)
  })

  it("surfaces backend conflicts instead of reporting a fake success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ success: false, error: { code: "revision_conflict", message: "Revision changed" } }, 409)))
    await expect(setPersonalMemoryStatus(record.memory_id, 2, "disabled")).rejects.toMatchObject({ status: 409, code: "revision_conflict" })
  })
})

