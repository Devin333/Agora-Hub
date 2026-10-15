import { beforeEach, describe, expect, it } from "vitest"
import { acceptAccountHistory, historyState, importGuestHistory, readResearchWorkspace, recordResearchVisit, rememberResearchActivity, removeResearchGroup, removeResearchMaterial, removeWorkspaceReport, researchResumeHref, saveComposerDraft, saveResearchGroup, saveResearchMaterial, saveResearchPrompt, saveReaderSelectionState, saveWorkspaceReport, selectHistoryOwner } from "./history"
import { validateWorkspace } from "./history-model"
import { researchQuestionHref } from "./entry"
import { validMaterial, type ResearchMaterial, type ResearchReportDraft } from "./workspace-items"
import { importLegacyReportDrafts, readReportDrafts } from "@/features/reports/lib/report-drafts"

const material: ResearchMaterial = { id: "paper-a", groupId: "group-a", kind: "paper", title: "Source paper", url: "https://arxiv.org/abs/2605.22343", notes: "Review methods", favorite: false, readingStatus: "unread", tags: [], createdAt: 1, updatedAt: 2 }
const report: ResearchReportDraft = { id: "report-a", groupId: "group-a", question: "Agent", title: "Agent review", scope: "Methods", notes: "Compare evidence", materials: [{ id: material.id, title: material.title, url: material.url, notes: material.notes }], updatedAt: 2 }

describe("owned research workspace", () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); selectHistoryOwner(null) })
  it("accepts private PDF identities without inventing reader links and rejects missing sources", () => {
    const pdf = { ...material, groupId: null, kind: "pdf", url: "", referenceId: "private-paper" }
    expect(validMaterial(pdf)).toBe(true)
    expect(validMaterial({ ...pdf, referenceId: undefined })).toBe(false)
    expect(validMaterial({ ...pdf, kind: "project" })).toBe(false)
  })
  it("migrates legacy material defaults and persists explicit library state", () => {
    const legacy: Record<string, unknown> = {
      id: "paper-legacy",
      groupId: null,
      kind: "paper",
      title: "Legacy paper",
      url: "https://example.test/legacy",
      notes: "",
      createdAt: 1,
      updatedAt: 1,
    }
    expect(validMaterial(legacy)).toBe(true)
    expect(legacy).toMatchObject({ favorite: false, readingStatus: "unread", tags: [] })

    const updated: ResearchMaterial = {
      ...(legacy as ResearchMaterial),
      favorite: true,
      readingStatus: "read",
      tags: ["RAG", "Methods"],
      updatedAt: 2,
    }
    expect(saveResearchMaterial(updated)).toBe(true)
    expect(readResearchWorkspace().materials).toEqual([updated])
    expect(validMaterial({ ...updated, tags: ["duplicate", "DUPLICATE"] })).toBe(false)
    expect(validMaterial({ ...updated, tags: [" leading"] })).toBe(false)
    expect(validMaterial({ ...updated, tags: Array.from({ length: 33 }, (_, index) => `tag-${index}`) })).toBe(false)
  })
  function populate() {
    saveResearchGroup("Agent", "group-a")
    expect(saveResearchMaterial(material)).toBe(true)
    expect(saveWorkspaceReport(report)).toBe(true)
    expect(saveResearchPrompt({ id: "prompt-a", name: "Recent Agent papers", question: "Agent", mode: "papers", constraints: { recentYear: true }, updatedAt: 2 })).toBe(true)
    expect(saveComposerDraft({ question: "Unsent", mode: "papers", groupId: "group-a", constraints: {}, updatedAt: 2 })).toBe(true)
  }
  it("persists revision-bound reader selections and rejects cross-revision events", () => {
    selectHistoryOwner("alice")
    acceptAccountHistory({ visits: [], groups: [] }, 0)
    const selection = {
      id: "selection-1",
      paperId: "2601.01234",
      sourceRevision: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      sectionId: "methods",
      paragraphId: "paragraph-1",
      pageNumber: 3,
      selectedText: "bounded retrieval",
      surroundingText: "uses bounded retrieval",
      startOffset: 5,
      endOffset: 22,
      noteText: "note",
      explainQuestion: "",
      exampleQuestion: "",
      explained: false,
      exampled: false,
      confused: true,
      createdAt: 1,
      updatedAt: 2,
    }
    const event = {
      id: "event-1",
      type: "selection_created" as const,
      paperId: selection.paperId,
      sourceRevision: selection.sourceRevision,
      selectionId: selection.id,
      sectionId: selection.sectionId,
      paragraphId: selection.paragraphId,
      pageNumber: 3,
      selectedText: selection.selectedText,
      surroundingText: selection.surroundingText,
      createdAt: 2,
    }
    expect(saveReaderSelectionState({
      paperId: selection.paperId,
      sourceRevision: selection.sourceRevision,
      selections: [selection],
      events: [event],
      updatedAt: 2,
    }, "alice")).toBe(true)
    const saved = readResearchWorkspace()
    expect(validateWorkspace(saved)).toEqual(saved)
    expect(saved.readerSelections?.[0]).not.toHaveProperty("blockId")
    expect(saved.readerSelections?.[0]).not.toHaveProperty("explainAnswer")
    expect(validateWorkspace({
      ...saved,
      readerSelections: [{ ...selection, blockId: null }],
    })).toBeNull()
    expect(validateWorkspace({
      ...saved,
      readerEvents: [{ ...event, sourceRevision: "other-revision" }],
    })).toBeNull()
  })

  it("restores legacy history and rejects dangling references instead of discarding only part of a snapshot", () => {
    expect(validateWorkspace({ visits: [], groups: [] })).toEqual({ visits: [], groups: [] })
    expect(validateWorkspace({ visits: [], groups: [], materials: [material] })).toBeNull()
    populate()
    const saved = readResearchWorkspace()
    expect(validateWorkspace(saved)).toEqual(saved)
    expect(validateWorkspace({ ...saved, materials: [material, material] })).toBeNull()
  })
  it("ungroups every content type without deleting notes, source identities or templates", () => {
    populate()
    removeResearchGroup("group-a")
    const workspace = readResearchWorkspace()
    expect(workspace.materials).toEqual([{ ...material, groupId: null, updatedAt: expect.any(Number) }])
    expect(workspace.reportDrafts?.[0]).toMatchObject({ groupId: null, notes: report.notes, materials: report.materials })
    expect(workspace.composerDraft).toMatchObject({ groupId: null, question: "Unsent" })
    expect(workspace.prompts?.[0].id).toBe("prompt-a")
  })
  it("uses an explicit existing group only when creating a new study", () => {
    saveResearchGroup("Agent", "group-a")
    const href = researchQuestionHref("papers", "Agent", "study-a") + "&researchGroup=group-a"
    recordResearchVisit(href)
    expect(readResearchWorkspace().visits[0].groupId).toBe("group-a")
    recordResearchVisit(researchQuestionHref("papers", "Agent", "study-b") + "&researchGroup=other-owner-group")
    expect(readResearchWorkspace().visits.find(v => v.id === "study-b")?.groupId).toBeNull()
  })
  it("validates composer ownership and unlinks a removed source without losing the question", () => {
    populate()
    const draft = { ...readResearchWorkspace().composerDraft!, materialIds: [material.id] }
    expect(saveComposerDraft({ ...draft, materialIds: ["other-owner-source"] })).toBe(false)
    expect(saveComposerDraft(draft)).toBe(true)
    expect(removeResearchMaterial(material.id)).toBe(true)
    expect(readResearchWorkspace().composerDraft).toMatchObject({ question: "Unsent", materialIds: [] })
  })
  it("does not discard a newer edit when an earlier navigation finally completes", () => {
    populate()
    const href = researchQuestionHref("papers", "Agent", "submitted-study")
    saveComposerDraft({ question: "New question", mode: "papers", groupId: null, constraints: {}, updatedAt: 2 })
    recordResearchVisit(href)
    expect(readResearchWorkspace().composerDraft?.question).toBe("New question")
  })
  it("resumes an exact source chapter while retaining the discovery query and filters", () => {
    const href = researchQuestionHref("papers", "Agent", "study-a") + "&has=code"
    recordResearchVisit(href)
    expect(rememberResearchActivity("study-a", { kind: "reader", paperId: "source-a", title: "Source", href: "/design-demo/papers/source-a/read", sourceRevision: "revision-1", sectionId: "methods", sectionTitle: "Methods", updatedAt: Date.now() })).toBe(true)
    const url = new URL(researchResumeHref(readResearchWorkspace().visits[0]), "https://agora.invalid")
    expect(url.pathname).toBe("/design-demo/papers/source-a/read")
    expect(url.searchParams.get("sourceRevision")).toBe("revision-1")
    expect(url.searchParams.get("resumeSection")).toBe("methods")
    expect(url.searchParams.get("returnTo")).toBe(href)
    expect(rememberResearchActivity("study-a", { kind: "report", draftId: "not-owned", title: "Other", updatedAt: 2 })).toBe(false)
  })
  it("accepts opaque reader IDs and rejects traversal or unowned reader queries", () => {
    const now = Date.now()
    const base = {
      id: "paper-reader",
      groupId: null,
      kind: "paper" as const,
      title: "Opaque paper",
      url: "https://example.test/paper",
      notes: "",
      createdAt: now,
      updatedAt: now,
    }
    expect(validMaterial({ ...base, readerHref: "/papers/2601.01234/read?sourceRevision=revision-1" })).toBe(true)
    expect(validMaterial({ ...base, readerHref: "/papers/doi%3A10.1%2Fpaper/read" })).toBe(true)
    expect(validMaterial({ ...base, readerHref: "/papers/%2E%2E/read" })).toBe(false)
    expect(validMaterial({ ...base, readerHref: "/papers/paper/read?owner=forged" })).toBe(false)
    expect(validMaterial({ ...base, readerHref: "//evil.test/papers/paper/read" })).toBe(false)
  })

  it("rejects new precise resume state without an exact source identity", () => {
    const href = researchQuestionHref("papers", "Agent", "study-legacy")
    recordResearchVisit(href)
    expect(rememberResearchActivity("study-legacy", {
      kind: "reader",
      paperId: "source-a",
      title: "Source",
      href: "/design-demo/papers/source-a/read",
      sectionId: "methods",
      updatedAt: Date.now(),
    })).toBe(false)
    expect(readResearchWorkspace().visits[0].activity).toBeNull()
  })
  it("clears only a deleted draft's continuation and keeps original research searchable", () => {
    populate()
    recordResearchVisit(researchQuestionHref("reports", "Agent", "study-a"))
    expect(rememberResearchActivity("study-a", { kind: "report", draftId: report.id, title: report.title, updatedAt: Date.now() })).toBe(true)
    expect(researchResumeHref(readResearchWorkspace().visits[0])).toContain("draft=report-a")
    removeWorkspaceReport(report.id)
    expect(readResearchWorkspace().visits[0].activity).toBeNull()
    expect(readResearchWorkspace().visits[0].question).toBe("Agent")
    expect(readResearchWorkspace().materials).toHaveLength(1)
  })
  it("imports guest collections explicitly, idempotently and without copying the unsent guest draft", () => {
    populate()
    selectHistoryOwner("alice"); acceptAccountHistory({ visits: [], groups: [] }, 0)
    expect(readResearchWorkspace().materials).toBeUndefined()
    expect(importGuestHistory()).toBe(true); expect(importGuestHistory()).toBe(true)
    expect(readResearchWorkspace().materials).toEqual([material])
    expect(readResearchWorkspace().reportDrafts).toEqual([report])
    expect(readResearchWorkspace().composerDraft).toBeUndefined()
    selectHistoryOwner("bob"); acceptAccountHistory({ visits: [], groups: [] }, 0)
    expect(saveWorkspaceReport(report, "alice")).toBe(false)
    expect(saveComposerDraft({ question: "Alice private", mode: "auto", constraints: {}, groupId: null, updatedAt: 2 }, "alice")).toBe(false)
    expect(readResearchWorkspace()).toEqual({ visits: [], groups: [] })
  })
  it("does not expose global legacy report drafts to an account without explicit import", () => {
    localStorage.setItem("agora-report-drafts:v1", JSON.stringify([{ ...report, groupId: undefined }]))
    selectHistoryOwner("alice"); acceptAccountHistory({ visits: [], groups: [] }, 0)
    expect(readReportDrafts()).toEqual([])
    expect(importLegacyReportDrafts("alice")).toBe(true)
    expect(readReportDrafts()).toMatchObject([{ id: report.id, groupId: null }])
    expect(localStorage.getItem("agora-report-drafts:v1")).not.toBeNull()
    expect(historyState().dirty).toBe(true)
  })
})


describe("durable import source identity", () => {
  it("round-trips a passed import receipt and rejects a malformed source fingerprint", () => {
    const sourceFingerprint = `sha256:${"a".repeat(64)}`
    const operation = { requestId: "request-import-owned", generation: `gen_${"b".repeat(32)}`, fingerprint: `sha256:${"c".repeat(64)}`, kind: "import", status: "passed", semantic: { kind: "import", filename: "owned.pdf", sizeBytes: 128, sourceRevision: sourceFingerprint, sourceFingerprint }, paperId: "owned-paper", sourceRevision: sourceFingerprint, sourceFingerprint, importId: "owned-import", createdAt: 1, updatedAt: 2 }
    const restored = validateWorkspace({ visits: [], groups: [], pendingOperations: [operation] })
    expect(restored?.pendingOperations?.[0]).toEqual(operation)
    expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [{ ...operation, sourceFingerprint: "not-a-sha" }] })).toBeNull()
  })
})

it("retains prepare output identity without rewriting its fixed input semantic", () => {
  const sourceFingerprint = "sha256:" + "a".repeat(64)
  const operation = { requestId: "request-prepare-output", generation: "gen_" + "b".repeat(32), fingerprint: "sha256:" + "c".repeat(64), kind: "prepare", status: "passed", semantic: { kind: "prepare", paperId: "paper-prepare", sourceUrl: "https://example.org/paper", sourceType: null }, paperId: "paper-prepare", sourceRevision: "parsed-revision", sourceFingerprint, createdAt: 1, updatedAt: 2 }
  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [operation] })?.pendingOperations?.[0]).toEqual(operation)
  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [{ ...operation, paperId: "other-paper" }] })).toBeNull()
})

it("does not let an import recovery change its admitted byte fingerprint or source revision", () => {
  const original = "sha256:" + "a".repeat(64)
  const operation = { requestId: "request-fixed-import", generation: "gen_" + "b".repeat(32), fingerprint: "sha256:" + "c".repeat(64), kind: "import", status: "passed", semantic: { kind: "import", filename: "paper.pdf", sizeBytes: 128, sourceRevision: original, sourceFingerprint: original }, paperId: "paper-import", sourceRevision: original, sourceFingerprint: original, importId: "import-fixed", createdAt: 1, updatedAt: 2 }
  for (const patch of [{ sourceRevision: "other-revision" }, { sourceFingerprint: "sha256:" + "d".repeat(64) }, { question: "not an ask operation" }]) {
    expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [{ ...operation, ...patch }] })).toBeNull()
  }
})


it("requires complete passed output identity for import and prepare receipts", () => {
  const fingerprint = "sha256:" + "a".repeat(64)
  const base = { requestId: "request-operation-contract", generation: "gen_" + "b".repeat(32), fingerprint: "sha256:" + "c".repeat(64), createdAt: 1, updatedAt: 2 }
  const imported = { ...base, kind: "import", status: "passed", semantic: { kind: "import", filename: "paper.pdf", sizeBytes: 128, sourceRevision: fingerprint, sourceFingerprint: fingerprint }, paperId: "paper-import", importId: "import-id", sourceRevision: fingerprint, sourceFingerprint: fingerprint }
  const prepared = { ...base, requestId: "request-prepare-contract", kind: "prepare", status: "passed", semantic: { kind: "prepare", paperId: "paper-prepare", sourceUrl: "https://example.org/paper", sourceType: null }, paperId: "paper-prepare", sourceRevision: "source-revision", sourceFingerprint: fingerprint }

  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [imported] })).not.toBeNull()
  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [{ ...imported, status: "unknown" }] })).toBeNull()
  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [{ ...imported, importId: undefined }] })).toBeNull()
  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [prepared] })).not.toBeNull()
  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [{ ...prepared, status: "needs_action" }] })).toBeNull()
  expect(validateWorkspace({ visits: [], groups: [], pendingOperations: [{ ...prepared, sourceFingerprint: undefined }] })).toBeNull()
})
