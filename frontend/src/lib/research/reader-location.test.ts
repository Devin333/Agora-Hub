import { describe, expect, it } from "vitest"
import { resolveCitationLocation, resolveRequestedReaderLocation } from "./reader-location"

const sections = new Set(["methods"])

describe("reader location source identity", () => {
  it("restores an exact revision location", () => {
    expect(resolveRequestedReaderLocation(
      "?sourceRevision=revision-1&resumeSection=methods&resumePage=3",
      "revision-1",
      sections,
      true,
    )).toEqual({ allowed: true, location: { sectionId: "methods", page: 3 } })
  })

  it("does not apply an old location to a same-named section in a new revision", () => {
    expect(resolveRequestedReaderLocation(
      "?sourceRevision=revision-1&resumeSection=methods&resumePage=3",
      "revision-2",
      sections,
      true,
    )).toEqual({ allowed: false, reason: "source_revision_mismatch" })
  })

  it("requires explicit source identity for precise resume", () => {
    expect(resolveRequestedReaderLocation(
      "?resumeSection=methods",
      "revision-1",
      sections,
      true,
    )).toEqual({ allowed: false, reason: "source_identity_unavailable" })
  })

  it("rejects citations from another revision even when the section id matches", () => {
    expect(resolveCitationLocation({
      id: "citation-1",
      label: "Methods",
      sourceType: "section",
      sourceRevision: "revision-1",
      sectionId: "methods",
      valid: true,
    }, "revision-2", sections, true)).toEqual({
      allowed: false,
      reason: "source_revision_mismatch",
    })
  })
})
