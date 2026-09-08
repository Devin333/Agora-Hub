import { beforeEach, describe, expect, it } from "vitest"
import { acceptAccountHistory, historyState, importGuestHistory, readResearchWorkspace, recordResearchVisit, rememberResearchActivity, removeResearchGroup, removeResearchMaterial, removeWorkspaceReport, researchResumeHref, saveComposerDraft, saveResearchGroup, saveResearchMaterial, saveResearchPrompt, saveWorkspaceReport, selectHistoryOwner } from "./history"
import { validateWorkspace } from "./history-model"
import { researchQuestionHref } from "./entry"
import { type ResearchMaterial, type ResearchReportDraft } from "./workspace-items"
import { importLegacyReportDrafts, readReportDrafts } from "@/features/reports/lib/report-drafts"

const material: ResearchMaterial = { id: "paper-a", groupId: "group-a", kind: "paper", title: "Source paper", url: "https://arxiv.org/abs/2605.22343", notes: "Review methods", createdAt: 1, updatedAt: 2 }
const report: ResearchReportDraft = { id: "report-a", groupId: "group-a", question: "Agent", title: "Agent review", scope: "Methods", notes: "Compare evidence", materials: [{ id: material.id, title: material.title, url: material.url, notes: material.notes }], updatedAt: 2 }

describe("owned research workspace", () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); selectHistoryOwner(null) })
  function populate() {
    saveResearchGroup("Agent", "group-a")
    expect(saveResearchMaterial(material)).toBe(true)
    expect(saveWorkspaceReport(report)).toBe(true)
    expect(saveResearchPrompt({ id: "prompt-a", name: "Recent Agent papers", question: "Agent", mode: "papers", constraints: { recentYear: true }, updatedAt: 2 })).toBe(true)
    expect(saveComposerDraft({ question: "Unsent", mode: "papers", groupId: "group-a", constraints: {}, updatedAt: 2 })).toBe(true)
  }
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
    expect(rememberResearchActivity("study-a", { kind: "reader", paperId: "source-a", title: "Source", href: "/design-demo/papers/source-a/read", sectionId: "methods", sectionTitle: "Methods", updatedAt: Date.now() })).toBe(true)
    const url = new URL(researchResumeHref(readResearchWorkspace().visits[0]), "https://agora.invalid")
    expect(url.pathname).toBe("/design-demo/papers/source-a/read")
    expect(url.searchParams.get("resumeSection")).toBe("methods")
    expect(url.searchParams.get("returnTo")).toBe(href)
    expect(rememberResearchActivity("study-a", { kind: "report", draftId: "not-owned", title: "Other", updatedAt: 2 })).toBe(false)
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
