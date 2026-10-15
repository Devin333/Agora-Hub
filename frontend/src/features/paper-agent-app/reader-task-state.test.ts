import { describe, expect, it } from "vitest"
import type { ReaderTaskActivity } from "@/features/portal/reader/personal-memory-task-context"
import { activeReaderRequestIds, readerTaskSnapshots, usedReaderMemoryRefs } from "./reader-task-state"

function activity(patch: Partial<ReaderTaskActivity>): ReaderTaskActivity {
  return { kind: "ask", requestId: "request-1", generation: "generation-1", status: "waiting", usedPersonalMemory: [], ...patch }
}

describe("reader task state", () => {
  it("keeps uncertain requests active until authoritative terminal state arrives", () => {
    expect(activeReaderRequestIds([
      activity({ requestId: "waiting", status: "waiting" }),
      activity({ requestId: "running", status: "running" }),
      activity({ requestId: "unknown", status: "unknown" }),
      activity({ requestId: "passed", status: "passed" }),
    ])).toEqual(["waiting", "running", "unknown"])
  })


  it("keeps request and generation identities attached to the latest task state", () => {
    expect(readerTaskSnapshots([
      activity({ kind: "ask", requestId: "request-1", generation: "generation-old", status: "waiting" }),
      activity({ kind: "ask", requestId: "request-1", generation: "generation-new", status: "unknown" }),
      activity({ kind: "analyze", requestId: "request-2", generation: "generation-2", status: "rejected" }),
    ])).toEqual([
      { kind: "ask", requestId: "request-1", generation: "generation-new", status: "unknown" },
      { kind: "analyze", requestId: "request-2", generation: "generation-2", status: "rejected" },
    ])
  })

  it("discloses the actual used memory id and revision without duplicating refs", () => {
    const ref = { memoryId: `pmem_${"a".repeat(32)}`, revision: 7 }
    expect(usedReaderMemoryRefs([
      activity({ status: "passed", usedPersonalMemory: [ref] }),
      activity({ kind: "analyze", requestId: "request-2", status: "passed", usedPersonalMemory: [ref] }),
    ])).toEqual([ref])
  })
})
