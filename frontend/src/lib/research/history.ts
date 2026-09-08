import { researchModuleForPath, safeResearchHref, type ResearchModule } from "./entry"

export type ResearchVisit = { id: string; module: ResearchModule; question: string; href: string; scrollY: number; updatedAt: number }
export const researchHistoryKey = "agora-research-history:v1"
export const researchHistoryEvent = "agora-research-history-changed"
const resumeKey = "agora-research-resume:v1"

function validateVisit(value: unknown): ResearchVisit | null {
  if (!value || typeof value !== "object") return null
  const visit = value as ResearchVisit
  const href = safeResearchHref(visit.href)
  if (!href || typeof visit.question !== "string" || !visit.question.trim() || visit.question.length > 2000) return null
  const url = new URL(href, "https://agora.invalid")
  if (researchModuleForPath(url.pathname) !== visit.module || visit.id !== `${visit.module}:${visit.question}` || url.searchParams.get("question") !== visit.question) return null
  if (!Number.isFinite(visit.updatedAt) || visit.updatedAt <= 0 || !Number.isFinite(visit.scrollY) || visit.scrollY < 0 || visit.scrollY > 1000000) return null
  return { id: visit.id, module: visit.module, question: visit.question, href, scrollY: visit.scrollY, updatedAt: visit.updatedAt }
}

export function readResearchHistory(): ResearchVisit[] {
  try {
    const text = localStorage.getItem(researchHistoryKey) ?? "[]"
    if (text.length > 250000) return []
    const raw: unknown = JSON.parse(text)
    if (!Array.isArray(raw)) return []
    return raw.map(validateVisit).filter((item): item is ResearchVisit => Boolean(item)).sort((a, b) => b.updatedAt - a.updatedAt).slice(0, 10)
  } catch { return [] }
}

function writeHistory(visits: ResearchVisit[]) {
  try {
    localStorage.setItem(researchHistoryKey, JSON.stringify(visits.slice(0, 10)))
    window.dispatchEvent(new Event(researchHistoryEvent))
  } catch { /* Optional browser storage must never block research. */ }
}

export function recordResearchVisit(href: string, scrollY = 0): void {
  const safeHref = safeResearchHref(href)
  if (!safeHref) return
  const url = new URL(safeHref, "https://agora.invalid")
  const researchModule = researchModuleForPath(url.pathname)
  const question = url.searchParams.get("question")
  if (!researchModule || !question || question.length > 2000) return
  const visit = validateVisit({ id: `${researchModule}:${question}`, module: researchModule, question, href: safeHref, scrollY, updatedAt: Date.now() })
  if (!visit) return
  writeHistory([visit, ...readResearchHistory().filter(item => item.id !== visit.id)])
}

export function removeResearchVisit(id?: string) {
  writeHistory(id === undefined ? [] : readResearchHistory().filter(item => item.id !== id))
}

export function prepareResearchResume(visit: ResearchVisit) {
  const valid = validateVisit(visit)
  if (!valid) return
  try { sessionStorage.setItem(resumeKey, JSON.stringify(valid)) } catch { /* URL still restores filters. */ }
}

export function takeResearchResume(href: string): ResearchVisit | null {
  try {
    const visit = validateVisit(JSON.parse(sessionStorage.getItem(resumeKey) ?? "null"))
    sessionStorage.removeItem(resumeKey)
    return visit?.href === href ? visit : null
  } catch { return null }
}
