import { describe, expect, it } from "vitest"
import {
  isPersonalMemoryId,
  personalMemoryExportPath,
  personalMemoryListPath,
  sanitizePersonalMemoryIndexRebuild,
  sanitizePersonalMemoryConfirm,
  sanitizePersonalMemoryCreate,
  sanitizePersonalMemoryPolicy,
  sanitizePersonalMemoryRecall,
  sanitizePersonalMemoryRevision,
  sanitizePersonalMemoryStatus,
  sanitizePersonalMemoryUpdate,
} from "@/lib/research/personal-memory-contracts"

const source = { source_type: "user_input", source_ref: "user://settings/memory" }

describe("personal memory contracts", () => {
  it("accepts explicit confirmed and candidate creation without owner scope", () => {
    const confirmed = {
      creation: "confirmed",
      request_id: "memory-request-1",
      content: "I prefer concise evidence summaries.",
      category: "reading_preference",
      source_refs: [source],
    }
    expect(sanitizePersonalMemoryCreate(confirmed)).toEqual(confirmed)
    expect(sanitizePersonalMemoryCreate({ ...confirmed, creation: "candidate" })).not.toBeNull()
    expect(sanitizePersonalMemoryCreate({ ...confirmed, user_id: "forged" })).toBeNull()
    expect(sanitizePersonalMemoryCreate({ ...confirmed, source_refs: [{ ...source, owner: "forged" }] })).toBeNull()
  })

  it("enforces CAS and rejects unknown or empty mutations", () => {
    expect(sanitizePersonalMemoryUpdate({ revision: 2, content: "Updated" })).toEqual({ revision: 2, content: "Updated" })
    expect(sanitizePersonalMemoryUpdate({ revision: 2 })).toBeNull()
    expect(sanitizePersonalMemoryUpdate({ revision: 0, content: "Updated" })).toBeNull()
    expect(sanitizePersonalMemoryConfirm({ revision: 1 })).toEqual({ revision: 1 })
    expect(sanitizePersonalMemoryIndexRebuild({})).toEqual({})
    expect(sanitizePersonalMemoryIndexRebuild({ owner: "forged" })).toBeNull()
    expect(sanitizePersonalMemoryRevision({ revision: 3 })).toEqual({ revision: 3 })
    expect(sanitizePersonalMemoryStatus({ status: "disabled", revision: 3 })).toEqual({ status: "disabled", revision: 3 })
    expect(sanitizePersonalMemoryStatus({ status: "deleted", revision: 3 })).toBeNull()
  })

  it("bounds recall and remote policy without accepting owner claims", () => {
    expect(sanitizePersonalMemoryRecall({ query: "agent replay", limit: 5, use_memory: true })).not.toBeNull()
    expect(sanitizePersonalMemoryRecall({ query: "agent replay", limit: 21 })).toBeNull()
    expect(sanitizePersonalMemoryPolicy({ allow_remote_processing: false, revision: 1 })).not.toBeNull()
    expect(sanitizePersonalMemoryPolicy({
      allow_remote_processing: false,
      remote_processing_permissions: { model: false, tool: false, embedding: false },
      revision: 1,
    })).toBeNull()
    expect(sanitizePersonalMemoryPolicy({ allow_remote_processing: true, revision: 1, tenantId: "forged" })).toBeNull()
  })

  it("validates ids and bounded list/export query forwarding", () => {
    expect(isPersonalMemoryId(`pmem_${"a".repeat(32)}`)).toBe(true)
    expect(isPersonalMemoryId("../../escape")).toBe(false)
    expect(personalMemoryListPath(new URLSearchParams("limit=20&cursor=40&status=active&status=disabled"))).toBe(
      "/api/v1/research/desktop/memory?limit=20&cursor=40&status=active&status=disabled",
    )
    expect(personalMemoryListPath(new URLSearchParams("owner=user-a"))).toBeNull()
    expect(personalMemoryListPath(new URLSearchParams("limit=101"))).toBeNull()
    expect(personalMemoryListPath(new URLSearchParams("cursor=10001"))).toBeNull()
    expect(personalMemoryListPath(new URLSearchParams("status=active&status=active"))).toBeNull()
    expect(personalMemoryExportPath(new URLSearchParams("include_deleted=false"))).toBe(
      "/api/v1/research/desktop/memory/export?include_deleted=false",
    )
    expect(personalMemoryExportPath(new URLSearchParams("include_deleted=yes"))).toBeNull()
  })
})
