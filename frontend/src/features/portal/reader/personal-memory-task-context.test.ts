import { beforeEach, describe, expect, it, vi } from "vitest"
import type { PersonalMemoryTaskPreview } from "@/lib/research/desktop/client"
import {
  boundedRecallQuery,
  resolveReaderPersonalMemory,
  usedPersonalMemoryIsCompatible,
  usedPersonalMemoryMatches,
} from "./personal-memory-task-context"

const mocks = vi.hoisted(() => ({ previewPersonalMemoryTask: vi.fn() }))
vi.mock("@/lib/research/desktop/client", () => ({
  previewPersonalMemoryTask: mocks.previewPersonalMemoryTask,
}))

const memoryId = `pmem_${"1".repeat(32)}`
const selection = {
  policyRevision: 2,
  refs: [{ memoryId, revision: 3 }],
}

function preview(reason: PersonalMemoryTaskPreview["reason"]): PersonalMemoryTaskPreview {
  return {
    policy: {
      policy: {
        allow_remote_processing: true,
        revision: 2,
        created_at: "2026-10-14T00:00:00+00:00",
        updated_at: "2026-10-14T00:00:00+00:00",
      },
    },
    recall: null,
    reason,
    ...(reason === "matched" ? { selection } : {}),
  }
}

describe("reader personal-memory task context", () => {
  beforeEach(() => vi.clearAllMocks())

  it("does not perform recall unless the per-task switch is explicitly enabled", async () => {
    expect(await resolveReaderPersonalMemory(undefined, ["question"])).toBeUndefined()
    expect(await resolveReaderPersonalMemory({ enabled: false }, ["question"])).toBeUndefined()
    expect(mocks.previewPersonalMemoryTask).not.toHaveBeenCalled()
  })

  it("builds a bounded contextual query and returns only exact recalled refs", async () => {
    mocks.previewPersonalMemoryTask.mockResolvedValue(preview("matched"))
    const onPreview = vi.fn()

    const resolved = await resolveReaderPersonalMemory(
      { enabled: true, limit: 50, onPreview },
      [" What is the method? ", "Paper title", "What is the method?", "x".repeat(600)],
    )

    expect(resolved).toEqual(selection)
    const input = mocks.previewPersonalMemoryTask.mock.calls[0][0]
    expect(input.limit).toBe(10)
    expect(input.useMemory).toBe(true)
    expect(input.query.length).toBeLessThanOrEqual(512)
    expect(input.query).toContain("What is the method?\nPaper title")
    expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ reason: "matched" }))
  })

  it("returns a detached frozen selection and rejects a policy revision mismatch", async () => {
    const matched = preview("matched")
    mocks.previewPersonalMemoryTask.mockResolvedValue(matched)

    const resolved = await resolveReaderPersonalMemory({ enabled: true }, ["question"])
    expect(resolved).toEqual(selection)
    expect(resolved).not.toBe(matched.selection)
    expect(resolved?.refs).not.toBe(matched.selection?.refs)

    mocks.previewPersonalMemoryTask.mockResolvedValue({
      ...preview("matched"),
      policy: {
        policy: {
          ...preview("matched").policy.policy,
          revision: 4,
        },
      },
    })
    await expect(
      resolveReaderPersonalMemory({ enabled: true }, ["question"]),
    ).rejects.toThrow("个人记忆召回回执无效")
  })

  it("requires returned used refs to match the frozen request snapshot exactly", () => {
    expect(usedPersonalMemoryMatches(selection, {
      usedPersonalMemory: [{ memoryId, revision: 3 }],
    })).toBe(true)
    expect(usedPersonalMemoryMatches(selection, {})).toBe(false)
    expect(usedPersonalMemoryMatches(selection, {
      usedPersonalMemory: [{ memoryId, revision: 4 }],
    })).toBe(false)
    expect(usedPersonalMemoryMatches(undefined, {
      usedPersonalMemory: [{ memoryId, revision: 3 }],
    })).toBe(false)
    expect(usedPersonalMemoryMatches(undefined, {
      usedPersonalMemory: [{ memoryId, revision: 3, content: "forged" }],
    })).toBe(false)
    expect(usedPersonalMemoryIsCompatible(selection, {})).toBe(true)
    expect(usedPersonalMemoryIsCompatible(selection, { usedPersonalMemory: [] })).toBe(true)
    expect(usedPersonalMemoryIsCompatible(undefined, {
      usedPersonalMemory: [{ memoryId, revision: 3 }],
    })).toBe(false)
  })

  it("normalizes duplicate query context without changing task semantics", () => {
    expect(boundedRecallQuery(["  methods   section ", "methods section", null, "paper"])).toBe(
      "methods section\npaper",
    )
  })
})
