import { researchModuleForPath, safeResearchHref } from "./entry"
import { conversationHref, validResearchConversation, type ResearchConversation } from "./conversation"
import { researchQuestionHref } from "./entry"
import { emptyWorkspace, legacyHistoryId, migrateLegacyHistory, sessionParameter, validateVisit, validateWorkspace, validHistoryId, type ResearchVisit, type ResearchWorkspace } from "./history-model"
import { validMaterial, validReportDraft, validPrompt, validActivity, type ResearchMaterial, type ResearchReportDraft, type ResearchPrompt, type ResearchComposerDraft, type ResearchActivity } from "./workspace-items"
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
/** Mutations from an open editor must retain the owner captured when it opened. */
export function updateResearchWorkspace(update: (workspace: ResearchWorkspace) => ResearchWorkspace, expectedOwner: string | null | undefined = owner): boolean {
  if (expectedOwner === undefined || expectedOwner !== owner) return false
  return saveWorkspace(update(readResearchWorkspace()))
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
  const requestedGroup = url.searchParams.get("researchGroup")
  const groupId = existing ? existing.groupId : workspace.groups.some(g => g.id === requestedGroup) ? requestedGroup : null
  const visit = validateVisit({ ...existing, id, module: researchModule, question, href: safeHref, scrollY, title: existing?.title ?? question.slice(0, 40), groupId, isFavorite: existing?.isFavorite ?? false, createdAt: existing?.createdAt ?? now, updatedAt: now, deletedAt: null })
  if (!visit) return
  // Clear only the draft handed off to this exact session, never a later edit.
  const acceptedDraft = workspace.composerDraft?.submittedSessionId === id && workspace.composerDraft.question.trim() === question
  saveWorkspace({ ...workspace, ...(acceptedDraft ? { composerDraft: null } : {}), visits: [visit, ...workspace.visits.filter(v => v.id !== id)] })
}
export function updateResearchVisit(id: string, patch: Partial<Pick<ResearchVisit, "title" | "groupId" | "isFavorite" | "archivedAt">>): boolean {
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
  const ungroup = <T extends { groupId?: string | null }>(item: T): T => item.groupId === id ? { ...item, groupId: null, updatedAt: Date.now() } : item
  return saveWorkspace({ ...workspace, groups: workspace.groups.filter(g => g.id !== id), visits: workspace.visits.map(ungroup),
    ...(workspace.materials ? { materials: workspace.materials.map(ungroup) } : {}),
    ...(workspace.reportDrafts ? { reportDrafts: workspace.reportDrafts.map(ungroup) } : {}),
    ...(workspace.composerDraft ? { composerDraft: ungroup(workspace.composerDraft) } : {}),
  })
}
export function importGuestHistory(): boolean {
  if (!owner) return false
  const guest = readGuestHistory(), workspace = readResearchWorkspace()
  const groups = [...workspace.groups, ...guest.groups.filter(g => !workspace.groups.some(item => item.id === g.id))]
  const merge = <T extends { id: string }>(owned: T[] = [], local: T[] = []) => [...owned, ...local.filter(item => !owned.some(existing => existing.id === item.id))]
  const reportDrafts = merge(workspace.reportDrafts, guest.reportDrafts)
  const visits = merge(workspace.visits, guest.visits.map(v => v.activity?.kind === "report" && workspace.reportDrafts?.some(d => d.id === (v.activity as Extract<ResearchActivity, { kind: "report" }>).draftId) ? { ...v, activity: null } : v))
  return saveWorkspace({ ...workspace, groups, visits, materials: merge(workspace.materials, guest.materials), reportDrafts, prompts: merge(workspace.prompts, guest.prompts) })
}

export function saveResearchMaterial(material: ResearchMaterial, expectedOwner = owner): boolean {
  if (!validMaterial(material)) return false
  return updateResearchWorkspace(workspace => ({ ...workspace, materials: [material, ...(workspace.materials ?? []).filter(m => m.id !== material.id)] }), expectedOwner)
}
export function removeResearchMaterial(id: string, expectedOwner = owner): boolean {
  return updateResearchWorkspace(workspace => ({ ...workspace, materials: (workspace.materials ?? []).filter(m => m.id !== id),
    ...(workspace.composerDraft?.materialIds?.includes(id) ? { composerDraft: { ...workspace.composerDraft, materialIds: workspace.composerDraft.materialIds.filter(item => item !== id) } } : {}),
  }), expectedOwner)
}
export function saveWorkspaceReport(draft: ResearchReportDraft, expectedOwner = owner): boolean {
  if (!validReportDraft(draft)) return false
  return updateResearchWorkspace(workspace => ({ ...workspace, reportDrafts: [draft, ...(workspace.reportDrafts ?? []).filter(d => d.id !== draft.id)] }), expectedOwner)
}
export function removeWorkspaceReport(id: string, expectedOwner = owner): boolean {
  return updateResearchWorkspace(workspace => ({ ...workspace, reportDrafts: (workspace.reportDrafts ?? []).filter(d => d.id !== id), visits: workspace.visits.map(v => v.activity?.kind === "report" && v.activity.draftId === id ? { ...v, activity: null } : v) }), expectedOwner)
}
export function saveResearchPrompt(prompt: ResearchPrompt, expectedOwner = owner): boolean {
  if (!validPrompt(prompt)) return false
  return updateResearchWorkspace(workspace => ({ ...workspace, prompts: [prompt, ...(workspace.prompts ?? []).filter(p => p.id !== prompt.id)] }), expectedOwner)
}
export function removeResearchPrompt(id: string, expectedOwner = owner): boolean {
  return updateResearchWorkspace(workspace => ({ ...workspace, prompts: (workspace.prompts ?? []).filter(p => p.id !== id) }), expectedOwner)
}
export function saveComposerDraft(draft: ResearchComposerDraft | null, expectedOwner = owner): boolean {
  return updateResearchWorkspace(workspace => ({ ...workspace, composerDraft: draft }), expectedOwner)
}
export function rememberResearchActivity(sessionId: string, activity: ResearchActivity, expectedOwner = owner): boolean {
  if (!validActivity(activity)) return false
  const visit = readResearchWorkspace().visits.find(v => v.id === sessionId && !v.deletedAt)
  if (!visit || (activity.kind === "reader" ? visit.module !== "papers" : visit.module !== "reports")) return false
  return updateResearchWorkspace(workspace => ({ ...workspace, visits: workspace.visits.map(v => v.id === sessionId ? { ...v, activity, updatedAt: Math.max(v.updatedAt, activity.updatedAt) } : v) }), expectedOwner)
}
export function researchResumeHref(visit: ResearchVisit): string {
  if (visit.conversation) return conversationHref(visit.id)
  const activity = visit.activity
  if (!activity) return visit.href
  if (activity.kind === "report") {
    if (!readResearchWorkspace().reportDrafts?.some(d => d.id === activity.draftId)) return visit.href
    const url = new URL(visit.href, "https://agora.invalid")
    url.searchParams.set("draft", activity.draftId); url.searchParams.set("compose", "1")
    return url.pathname + url.search
  }
  const url = new URL(activity.href, "https://agora.invalid")
  url.searchParams.set("returnTo", visit.href)
  if (activity.sectionId) url.searchParams.set("resumeSection", activity.sectionId)
  if (activity.pdfPage) url.searchParams.set("resumePage", String(activity.pdfPage))
  return url.pathname + url.search
}

/** Atomic conversation + visit update retains a single history identity across follow-ups. */
export function saveResearchConversation(id: string, conversation: ResearchConversation, groupId: string | null, expectedOwner: string | null | undefined, scrollY?: number): boolean {
  if (!validHistoryId(id) || !validResearchConversation(conversation)) return false
  return updateResearchWorkspace(workspace => {
    const existing = workspace.visits.find(visit => visit.id === id)
    if (existing?.deletedAt) return workspace
    const question = conversation.turns[0].question, now = Date.now()
    const researchModule = existing?.module ?? conversation.turns[0].intent?.sources[0] ?? "papers"
    const visit: ResearchVisit = { id, module: researchModule, question, href: researchQuestionHref(researchModule, question, id), title: existing?.title ?? question.slice(0, 40), groupId: existing ? existing.groupId : groupId, isFavorite: existing?.isFavorite ?? false, createdAt: existing?.createdAt ?? now, deletedAt: null, ...existing, updatedAt: now, conversation, scrollY: scrollY ?? existing?.scrollY ?? 0 }
    return { ...workspace, ...(!existing && workspace.composerDraft?.question.trim() === question ? { composerDraft: null } : {}), visits: [visit, ...workspace.visits.filter(item => item.id !== id)] }
  }, expectedOwner)
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
