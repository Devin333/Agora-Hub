import { historyState, readResearchWorkspace, researchHistoryEvent, saveWorkspaceReport, removeWorkspaceReport, updateResearchWorkspace } from "@/lib/research/history"
import { safeWorkspaceSource, validReportDraft, type ResearchReportDraft, type ResearchReportMaterial } from "@/lib/research/workspace-items"
export type ReportMaterial = ResearchReportMaterial
export type ReportDraft = ResearchReportDraft
const key = "agora-report-drafts:v1"
export const reportDraftEvent = researchHistoryEvent

export function safeMaterialUrl(value: string): boolean {
  return safeWorkspaceSource(value)
}
export function readReportDrafts(): ReportDraft[] {
  return (readResearchWorkspace().reportDrafts ?? []).slice().sort((a, b) => b.updatedAt - a.updatedAt)
}
export function readLegacyReportDrafts(): ReportDraft[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw || raw.length > 3000000) return []
    const items: unknown = JSON.parse(raw)
    return Array.isArray(items) ? items.filter(validReportDraft).map(item => ({ ...item, groupId: null })).sort((a, b) => b.updatedAt - a.updatedAt).slice(0, 20) : []
  } catch { return [] }
}
export function saveReportDraft(draft: ReportDraft, expectedOwner = historyState().owner): boolean {
  return saveWorkspaceReport(draft, expectedOwner)
}
export function deleteReportDraft(id: string, expectedOwner = historyState().owner): boolean {
  return removeWorkspaceReport(id, expectedOwner)
}
export function importLegacyReportDrafts(expectedOwner = historyState().owner): boolean {
  return updateResearchWorkspace(workspace => ({ ...workspace, reportDrafts: [...(workspace.reportDrafts ?? []), ...readLegacyReportDrafts().filter(d => !workspace.reportDrafts?.some(existing => existing.id === d.id))] }), expectedOwner)
}
export function newReportDraft(question: string): ReportDraft {
  return { id: crypto.randomUUID(), question, title: question, scope: "", notes: "", materials: [], updatedAt: Date.now() }
}
