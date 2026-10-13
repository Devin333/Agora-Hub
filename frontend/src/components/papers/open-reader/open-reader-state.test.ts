import { describe, expect, it } from "vitest"
import { selectionScope } from "./open-reader-state"

describe("owner-scoped reader selections", () => {
  it("changes persistence scope for owner, paper and exact source revision", () => {
    const alice = selectionScope(true, "alice", "2601.01234", "revision-1")
    expect(selectionScope(true, "bob", "2601.01234", "revision-1")).not.toBe(alice)
    expect(selectionScope(true, "alice", "2601.01234", "revision-2")).not.toBe(alice)
    expect(selectionScope(true, "alice", "2602.00001", "revision-1")).not.toBe(alice)
  })

  it("never treats a missing source revision as the exact revision scope", () => {
    expect(selectionScope(true, "alice", "2601.01234", undefined)).not.toBe(
      selectionScope(true, "alice", "2601.01234", "revision-1"),
    )
  })
})
