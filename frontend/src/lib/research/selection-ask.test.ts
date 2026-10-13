import { describe, expect, it } from "vitest"
import { matchesSelectionAnswerScope } from "./selection-ask"

const answer = {
  requestKey: "request-1",
  selectionId: "selection-1",
  mode: "explain" as const,
  question: "为什么？",
  answer: "因为证据支持。",
  selection: { sectionId: "methods", selectedText: "原文" },
  owner: "alice",
  sourceRevision: "revision-1",
}

describe("grounded selection answer ownership", () => {
  it("accepts only the current owner, revision and request identity", () => {
    expect(matchesSelectionAnswerScope(answer, "alice", "revision-1", "request-1")).toBe(true)
    expect(matchesSelectionAnswerScope(answer, "bob", "revision-1", "request-1")).toBe(false)
    expect(matchesSelectionAnswerScope(answer, "alice", "revision-2", "request-1")).toBe(false)
    expect(matchesSelectionAnswerScope(answer, "alice", "revision-1", "request-2")).toBe(false)
  })

  it("rejects an unverified source instead of inventing a revision", () => {
    expect(matchesSelectionAnswerScope(answer, "alice", undefined)).toBe(false)
    expect(matchesSelectionAnswerScope(answer, "alice", "")).toBe(false)
  })
})
