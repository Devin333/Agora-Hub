export type ReportMaterial = { id: string; title: string; url: string; notes: string }
export type ReportDraft = { id: string; question: string; title: string; scope: string; notes: string; materials: ReportMaterial[]; updatedAt: number }
const key = "agora-report-drafts:v1"
export const reportDraftEvent = "agora-report-drafts-changed"

export function safeMaterialUrl(value: string): boolean {
  if (!value.trim()) return true
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password }
  catch { return false }
}
function boundedText(value: unknown, max: number): value is string { return typeof value === "string" && value.length <= max }
function validDraft(value: unknown): value is ReportDraft {
  if (!value || typeof value !== "object") return false
  const item = value as ReportDraft
  return boundedText(item.id, 100) && Boolean(item.id) && boundedText(item.question, 2000) && boundedText(item.title, 2000) && Boolean(item.title.trim()) && boundedText(item.scope, 10000) && boundedText(item.notes, 30000) && Number.isFinite(item.updatedAt) && item.updatedAt > 0 && Array.isArray(item.materials) && item.materials.length <= 30 && item.materials.every(material => material && boundedText(material.id, 100) && boundedText(material.title, 500) && boundedText(material.url, 2000) && safeMaterialUrl(material.url) && boundedText(material.notes, 5000))
}
export function readReportDrafts(): ReportDraft[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw || raw.length > 3000000) return []
    const items: unknown = JSON.parse(raw)
    return Array.isArray(items) ? items.filter(validDraft).sort((a, b) => b.updatedAt - a.updatedAt).slice(0, 20) : []
  } catch { return [] }
}
export function saveReportDraft(draft: ReportDraft): boolean {
  if (!validDraft(draft)) return false
  try {
    localStorage.setItem(key, JSON.stringify([draft, ...readReportDrafts().filter(item => item.id !== draft.id)].slice(0, 20)))
    window.dispatchEvent(new Event(reportDraftEvent))
    return true
  } catch { return false }
}
export function deleteReportDraft(id: string): boolean {
  try {
    localStorage.setItem(key, JSON.stringify(readReportDrafts().filter(item => item.id !== id)))
    window.dispatchEvent(new Event(reportDraftEvent))
    return true
  } catch { return false }
}
export function newReportDraft(question: string): ReportDraft {
  return { id: crypto.randomUUID(), question, title: question, scope: "", notes: "", materials: [], updatedAt: Date.now() }
}
