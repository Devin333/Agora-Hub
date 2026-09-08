import { researchModuleForPath, safeResearchHref } from "./entry"
import { emptyWorkspace, legacyHistoryId, migrateLegacyHistory, sessionParameter, validateVisit, validateWorkspace, validHistoryId, type ResearchVisit, type ResearchWorkspace } from "./history-model"
export type { ResearchVisit, ResearchGroup, ResearchWorkspace } from "./history-model"

export const researchHistoryKey = "agora-research-history:v2"
export const researchHistoryLegacyKey = "agora-research-history:v1"
export const researchHistoryEvent = "agora-research-history-changed"
const resumeKey = "agora-research-resume:v2"
export type HistoryStatus = "local" | "loading" | "saving" | "synced" | "error" | "conflict"
let owner: string | null | undefined = null
let accountWorkspace = emptyWorkspace()
let status: HistoryStatus = "local"
let message = ""
let revision = 0
let generation = 0
let dirty = false
let loaded = true

export function notifyResearchHistory() { if (typeof window !== "undefined") window.dispatchEvent(new Event(researchHistoryEvent)) }
export function historyState() { return { owner, status, message, revision, generation, dirty, ready: loaded && owner !== undefined, workspace: readResearchWorkspace() } }
export function readGuestHistory(): ResearchWorkspace {
  try {
    const text = localStorage.getItem(researchHistoryKey)
    if (text !== null) {
      if (text.length > 8000000) return emptyWorkspace()
      return validateWorkspace(JSON.parse(text)) ?? emptyWorkspace()
    }
    const legacy = localStorage.getItem(researchHistoryLegacyKey) ?? "[]"
    return legacy.length > 250000 ? emptyWorkspace() : migrateLegacyHistory(JSON.parse(legacy))
  } catch { return emptyWorkspace() }
}
export function readResearchWorkspace() { return owner === null ? readGuestHistory() : accountWorkspace }
export function readResearchHistory(): ResearchVisit[] { return readResearchWorkspace().visits.filter(v => !v.deletedAt).sort((a, b) => b.updatedAt - a.updatedAt) }
export function setHistoryStatus(next: HistoryStatus, detail = "") { status = next; message = detail; notifyResearchHistory() }
export function selectHistoryOwner(next: string | null | undefined) {
  owner = next; accountWorkspace = emptyWorkspace(); revision = 0; dirty = false; loaded = next === null; generation++
  setHistoryStatus(next === null ? "local" : "loading")
}
export function acceptAccountHistory(workspace: ResearchWorkspace, nextRevision: number) {
  accountWorkspace = workspace; revision = nextRevision; dirty = false; loaded = true; generation++; setHistoryStatus("synced")
}
export function acknowledgeAccountHistory(nextRevision: number, savedGeneration: number) {
  revision = nextRevision
  if (generation === savedGeneration) dirty = false
  persistPendingHistory()
  setHistoryStatus(dirty ? "saving" : "synced")
}
export const pendingHistoryKey = (userId: string) => `agora-research-pending:${userId}`
function persistPendingHistory() {
  if (!owner) return
  try {
    if (dirty) sessionStorage.setItem(pendingHistoryKey(owner), JSON.stringify({ ...accountWorkspace, revision }))
    else sessionStorage.removeItem(pendingHistoryKey(owner))
  } catch { /* In-memory edits are retained; the UI continues to show pending sync. */ }
}
export function restorePendingHistory(workspace: ResearchWorkspace, baseRevision: number) {
  accountWorkspace = workspace; revision = baseRevision; loaded = true; dirty = true; generation++; setHistoryStatus("saving")
}
function saveWorkspace(workspace: ResearchWorkspace): boolean {
  if (!historyState().ready) { message = "历史正在加载，请稍后重试。"; notifyResearchHistory(); return false }
  const valid = validateWorkspace(workspace)
  if (!valid) { setHistoryStatus("error", "记录数量已达上限或内容无效，请整理后重试。"); return false }
  if (new TextEncoder().encode(JSON.stringify(valid)).byteLength > 4000000) { setHistoryStatus("error", "历史内容过多，暂时无法保存。请先导出备份并整理记录。"); return false }
  if (owner === null) {
    try { localStorage.setItem(researchHistoryKey, JSON.stringify(valid)); status = "local"; message = "" }
    catch { setHistoryStatus("error", "浏览器无法保存历史，请检查存储权限或可用空间。"); return false }
  } else { accountWorkspace = valid; dirty = true; if (status !== "conflict") status = "saving" }
  generation++; persistPendingHistory(); notifyResearchHistory(); return true
}
export function recordResearchVisit(href: string, scrollY = 0): void {
  if (!historyState().ready) return
  const safeHref = safeResearchHref(href)
  if (!safeHref) return
  const url = new URL(safeHref, "https://agora.invalid"), researchModule = researchModuleForPath(url.pathname), question = url.searchParams.get("question")
  if (!researchModule || !question?.trim() || question.length > 2000) return
  const identity = url.searchParams.get(sessionParameter)
  if (identity && !validHistoryId(identity)) return
  const id = identity ?? legacyHistoryId(`${researchModule}:${question}`)
  const workspace = readResearchWorkspace(), existing = workspace.visits.find(v => v.id === id)
  if (existing?.deletedAt) return
  const now = Date.now()
  const visit = validateVisit({ ...existing, id, module: researchModule, question, href: safeHref, scrollY, title: existing?.title ?? question.slice(0, 40), groupId: existing?.groupId ?? null, isFavorite: existing?.isFavorite ?? false, createdAt: existing?.createdAt ?? now, updatedAt: now, deletedAt: null })
  if (!visit) return
  saveWorkspace({ ...workspace, visits: [visit, ...workspace.visits.filter(v => v.id !== id)] })
}
export function updateResearchVisit(id: string, patch: Partial<Pick<ResearchVisit, "title" | "groupId" | "isFavorite">>): boolean {
  const workspace = readResearchWorkspace()
  return saveWorkspace({ ...workspace, visits: workspace.visits.map(v => v.id === id ? { ...v, ...patch, updatedAt: Date.now() } : v) })
}
export function removeResearchVisit(id?: string): boolean {
  const workspace = readResearchWorkspace(), now = Date.now()
  return saveWorkspace({ ...workspace, visits: workspace.visits.map(v => id === undefined || v.id === id ? { ...v, deletedAt: now, updatedAt: now } : v) })
}
export function restoreResearchVisit(id: string): boolean {
  const workspace = readResearchWorkspace()
  return saveWorkspace({ ...workspace, visits: workspace.visits.map(v => v.id === id ? { ...v, deletedAt: null, updatedAt: Date.now() } : v) })
}
export function saveResearchGroup(name: string, id = crypto.randomUUID()): string | null {
  const workspace = readResearchWorkspace(), cleaned = name.trim(), now = Date.now()
  if (!cleaned || cleaned.length > 60) { setHistoryStatus("error", "分组名称需要 1–60 个字。"); return null }
  if (workspace.groups.some(g => g.id !== id && g.name.toLocaleLowerCase() === cleaned.toLocaleLowerCase())) { setHistoryStatus("error", "已有同名分组，请换一个名称。"); return null }
  const existing = workspace.groups.find(g => g.id === id)
  return saveWorkspace({ ...workspace, groups: [...workspace.groups.filter(g => g.id !== id), { id, name: cleaned, createdAt: existing?.createdAt ?? now, updatedAt: now }] }) ? id : null
}
export function removeResearchGroup(id: string): boolean {
  const workspace = readResearchWorkspace()
  return saveWorkspace({ groups: workspace.groups.filter(g => g.id !== id), visits: workspace.visits.map(v => v.groupId === id ? { ...v, groupId: null, updatedAt: Date.now() } : v) })
}
export function importGuestHistory(): boolean {
  if (!owner) return false
  const guest = readGuestHistory(), workspace = readResearchWorkspace()
  const groups = [...workspace.groups, ...guest.groups.filter(g => !workspace.groups.some(item => item.id === g.id))]
  return saveWorkspace({ groups, visits: [...workspace.visits, ...guest.visits.filter(v => !workspace.visits.some(item => item.id === v.id))] })
}
export function prepareResearchResume(visit: ResearchVisit) {
  const valid = validateVisit(visit)
  if (!valid) return
  try { sessionStorage.setItem(resumeKey, JSON.stringify({ owner, visit: valid })) } catch { /* URL still restores filters. */ }
}
export function takeResearchResume(href: string): ResearchVisit | null {
  try {
    const raw = JSON.parse(sessionStorage.getItem(resumeKey) ?? "null")
    sessionStorage.removeItem(resumeKey)
    const visit = raw?.owner === owner ? validateVisit(raw.visit) : null
    return visit?.href === href ? visit : null
  } catch { return null }
}
