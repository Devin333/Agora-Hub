import { researchModuleForPath, safeResearchHref, type ResearchModule } from "./entry"
import { validActivity, validComposerDraft, validMaterial, validPrompt, validReportDraft, type ResearchActivity, type ResearchComposerDraft, type ResearchMaterial, type ResearchPrompt, type ResearchReportDraft } from "./workspace-items"

export type ResearchVisit = {
  id: string; module: ResearchModule; question: string; href: string; scrollY: number
  title: string; groupId: string | null; isFavorite: boolean
  createdAt: number; updatedAt: number; deletedAt: number | null
  archivedAt?: number | null
  activity?: ResearchActivity | null
}
export type ResearchGroup = { id: string; name: string; createdAt: number; updatedAt: number }
export type ResearchWorkspace = {
  visits: ResearchVisit[]; groups: ResearchGroup[]
  materials?: ResearchMaterial[]; reportDrafts?: ResearchReportDraft[]; prompts?: ResearchPrompt[]
  composerDraft?: ResearchComposerDraft | null
}
export type ResearchSnapshot = ResearchWorkspace & { revision: number }
export const emptyWorkspace = (): ResearchWorkspace => ({ visits: [], groups: [] })
export const sessionParameter = "researchSession"
export const validHistoryId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9:_-]{1,128}$/.test(value)
const validTime = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value > 0

export function validateVisit(value: unknown): ResearchVisit | null {
  if (!value || typeof value !== "object") return null
  const v = value as ResearchVisit
  const href = safeResearchHref(v.href)
  if (!href || !validHistoryId(v.id) || typeof v.question !== "string" || !v.question.trim() || v.question.length > 2000) return null
  const url = new URL(href, "https://agora.invalid")
  if (researchModuleForPath(url.pathname) !== v.module || url.searchParams.get("question") !== v.question) return null
  if (url.searchParams.getAll("question").length !== 1 || url.searchParams.getAll(sessionParameter).length > 1 || (url.searchParams.has(sessionParameter) && url.searchParams.get(sessionParameter) !== v.id)) return null
  if (!validTime(v.updatedAt) || !validTime(v.createdAt) || !Number.isFinite(v.scrollY) || v.scrollY < 0 || v.scrollY > 1000000) return null
  if (v.updatedAt < v.createdAt) return null
  if (typeof v.title !== "string" || !v.title.trim() || v.title.length > 120 || typeof v.isFavorite !== "boolean") return null
  if (v.groupId !== null && !validHistoryId(v.groupId)) return null
  if (v.deletedAt !== null && !validTime(v.deletedAt)) return null
  if (v.archivedAt != null && !validTime(v.archivedAt)) return null
  if (v.activity != null && !validActivity(v.activity)) return null
  if (v.activity?.kind === "reader" && v.module !== "papers") return null
  if (v.activity?.kind === "report" && v.module !== "reports") return null
  return { id: v.id, module: v.module, question: v.question, href, scrollY: v.scrollY, title: v.title.trim(), groupId: v.groupId, isFavorite: v.isFavorite, createdAt: v.createdAt, updatedAt: v.updatedAt, deletedAt: v.deletedAt, ...(v.archivedAt !== undefined ? { archivedAt: v.archivedAt } : {}), ...(v.activity !== undefined ? { activity: v.activity } : {}) }
}

export function validateWorkspace(value: unknown): ResearchWorkspace | null {
  if (!value || typeof value !== "object") return null
  const raw = value as ResearchWorkspace
  if (!Array.isArray(raw.visits) || !Array.isArray(raw.groups) || raw.visits.length > 2000 || raw.groups.length > 100) return null
  const groups: ResearchGroup[] = []
  for (const g of raw.groups) {
    if (!g || !validHistoryId(g.id) || typeof g.name !== "string" || !g.name.trim() || g.name.length > 60 || !validTime(g.createdAt) || !validTime(g.updatedAt) || g.updatedAt < g.createdAt || groups.some(item => item.id === g.id)) return null
    groups.push({ id: g.id, name: g.name.trim(), createdAt: g.createdAt, updatedAt: g.updatedAt })
  }
  const visits: ResearchVisit[] = []
  for (const value of raw.visits) {
    const visit = validateVisit(value)
    if (!visit || visits.some(item => item.id === visit.id) || (visit.groupId && !groups.some(g => g.id === visit.groupId))) return null
    visits.push(visit)
  }
  const groupExists = (id: string | null | undefined) => !id || groups.some(g => g.id === id)
  if (raw.materials !== undefined && (!Array.isArray(raw.materials) || raw.materials.length > 1000 || !raw.materials.every(m => validMaterial(m) && groupExists(m.groupId)) || new Set(raw.materials.map(m => m.id)).size !== raw.materials.length)) return null
  if (raw.reportDrafts !== undefined && (!Array.isArray(raw.reportDrafts) || raw.reportDrafts.length > 100 || !raw.reportDrafts.every(d => validReportDraft(d) && groupExists(d.groupId)) || new Set(raw.reportDrafts.map(d => d.id)).size !== raw.reportDrafts.length)) return null
  if (raw.prompts !== undefined && (!Array.isArray(raw.prompts) || raw.prompts.length > 100 || !raw.prompts.every(validPrompt) || new Set(raw.prompts.map(p => p.id)).size !== raw.prompts.length)) return null
  if (raw.composerDraft != null && (!validComposerDraft(raw.composerDraft) || !groupExists(raw.composerDraft.groupId))) return null
  if (raw.composerDraft?.materialIds?.some(id => !raw.materials?.some(m => m.id === id))) return null
  if (visits.some(v => v.activity?.kind === "report" && !(raw.reportDrafts ?? []).some(d => d.id === (v.activity as Extract<ResearchActivity, { kind: "report" }>).draftId))) return null
  return { visits, groups, ...(raw.materials !== undefined ? { materials: raw.materials } : {}), ...(raw.reportDrafts !== undefined ? { reportDrafts: raw.reportDrafts } : {}), ...(raw.prompts !== undefined ? { prompts: raw.prompts } : {}), ...(raw.composerDraft !== undefined ? { composerDraft: raw.composerDraft } : {}) }
}

// Deterministic identity deduplicates the same v1 record across imports.
export function legacyHistoryId(value: string): string {
  let first = 2166136261, second = 5381
  for (let i = 0; i < value.length; i++) { first = Math.imul(first ^ value.charCodeAt(i), 16777619); second = Math.imul(second, 33) ^ value.charCodeAt(i) }
  return `legacy-${(first >>> 0).toString(16)}-${(second >>> 0).toString(16)}`
}

export function migrateLegacyHistory(raw: unknown): ResearchWorkspace {
  if (!Array.isArray(raw)) return emptyWorkspace()
  const visits = raw.slice(0, 100).flatMap(v => {
    if (!v || typeof v.question !== "string" || v.id !== `${v.module}:${v.question}`) return []
    const visit = validateVisit({ ...v, id: legacyHistoryId(v.id), title: v.question.slice(0, 40), groupId: null, isFavorite: false, createdAt: v.updatedAt, deletedAt: null })
    return visit ? [visit] : []
  })
  return { visits: visits.filter((v, index) => visits.findIndex(item => item.id === v.id) === index), groups: [] }
}
