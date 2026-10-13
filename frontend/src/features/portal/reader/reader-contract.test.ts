import { describe, expect, it } from "vitest"
import type { ReaderWorkspacePayload } from "./reader-contract"
import {
  hasOwnedAnalysisSource,
  needsOwnedSourcePreparation,
} from "./reader-contract"

const publicReader = {
  paper: {
    id: "2601.01234",
    slug: "2601.01234",
    title: "Public paper",
    abstractSnippet: "",
    authors: [],
    publishedAt: "2026-01-12",
    tags: [],
    taskRefs: [],
    methodRefs: [],
    isPublished: true,
  },
  reader: null,
  state: "ready",
  sourceRevision: "public-source-hash",
  sourceFingerprint: `sha256:${"a".repeat(64)}`,
  aiReady: false,
} satisfies ReaderWorkspacePayload

describe("owned reader preparation eligibility", () => {
  it("does not treat a public reading hash as an owned analysis capability", () => {
    expect(hasOwnedAnalysisSource(publicReader)).toBe(false)
    expect(needsOwnedSourcePreparation(publicReader, true)).toBe(true)
  })

  it("stops offering preparation after the owned reader grants analyze capability", () => {
    const ownedReader: ReaderWorkspacePayload = {
      ...publicReader,
      capabilities: { analyze: true, ask: false },
    }
    expect(hasOwnedAnalysisSource(ownedReader)).toBe(true)
    expect(needsOwnedSourcePreparation(ownedReader, true)).toBe(false)
  })

  it("does not rerun public preparation for an existing owned reader projection", () => {
    const ownedPending: ReaderWorkspacePayload = {
      ...publicReader,
      capabilities: { analyze: false, ask: false },
    }
    expect(hasOwnedAnalysisSource(ownedPending)).toBe(false)
    expect(needsOwnedSourcePreparation(ownedPending, true)).toBe(false)
  })

  it("keeps the existing non-desktop reader behavior", () => {
    expect(needsOwnedSourcePreparation(publicReader, false)).toBe(false)
  })
})
