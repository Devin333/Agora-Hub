import { historyState, readResearchWorkspace, saveWorkspaceReport } from "./history"
import type { ResearchMaterial, ResearchReportDraft } from "./workspace-items"

export const materialKindLabel = { paper: "论文", project: "项目", discussion: "讨论", note: "笔记", pdf: "PDF" }
export function groupHref(id: string | null) { return `/design-demo/groups/${encodeURIComponent(id ?? "ungrouped")}` }

export function prepareMaterialReport(title: string, groupId: string | null, ids: string[], expectedOwner = historyState().owner): ResearchReportDraft | null {
  if (expectedOwner !== historyState().owner || !historyState().ready || !title.trim()) return null
  const workspace = readResearchWorkspace()
  if (groupId && !workspace.groups.some(g => g.id === groupId)) return null
  const uniqueIds = new Set(ids)
  const materials = (workspace.materials ?? []).filter(m => uniqueIds.has(m.id))
  if (materials.length !== uniqueIds.size || materials.length > 100) return null
  const draft: ResearchReportDraft = { id: crypto.randomUUID(), groupId, question: title, title, scope: "", notes: "", materials: materials.map(m => ({ id: m.id, title: m.title, url: m.url, notes: m.notes })), updatedAt: Date.now() }
  return saveWorkspaceReport(draft, expectedOwner) ? draft : null
}

export function reportDraftHref(draft: ResearchReportDraft, sessionId = crypto.randomUUID()): string {
  const params = new URLSearchParams({ question: draft.question || draft.title, draft: draft.id, compose: "1", researchSession: sessionId })
  if (draft.groupId) params.set("researchGroup", draft.groupId)
  return `/reports?${params}`
}

/** URLs identify sources, not permission. Only current-owner material IDs become context. */
export function selectedWorkspaceMaterials(search: string, available: ResearchMaterial[]): ResearchMaterial[] {
  const ids = new URLSearchParams(search).getAll("material")
  return available.filter(m => ids.includes(m.id))
}
