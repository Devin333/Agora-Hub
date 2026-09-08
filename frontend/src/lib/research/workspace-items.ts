import { isResearchMode, type ResearchMode } from "./entry"

export type ResearchConstraints = {
  recentYear?: boolean
  hasCode?: boolean
  paperType?: "survey"
  language?: "python" | "typescript" | "javascript" | "rust" | "go"
  license?: "MIT" | "Apache-2.0" | "BSD-3-Clause"
  recentlyActive?: boolean
  localRunnable?: boolean
}
export type ResearchMaterial = {
  id: string
  groupId: string | null
  kind: "paper" | "project" | "discussion" | "note" | "pdf"
  title: string
  url: string
  referenceId?: string
  readerHref?: string
  notes: string
  createdAt: number
  updatedAt: number
}
export type ResearchReportMaterial = { id: string; title: string; url: string; notes: string }
export type ResearchReportDraft = {
  id: string
  groupId?: string | null
  question: string
  title: string
  scope: string
  notes: string
  materials: ResearchReportMaterial[]
  updatedAt: number
}
export type ResearchPrompt = { id: string; name: string; question: string; mode: ResearchMode; constraints: ResearchConstraints; updatedAt: number }
export type ResearchComposerDraft = {
  question: string; mode: ResearchMode; constraints: ResearchConstraints; groupId: string | null; updatedAt: number
  materialIds?: string[]
  submittedSessionId?: string
}
export type ResearchActivity =
  | { kind: "reader"; paperId: string; title: string; href: string; sectionId?: string; sectionTitle?: string; pdfPage?: number; updatedAt: number }
  | { kind: "report"; draftId: string; title: string; updatedAt: number }

export const validWorkspaceId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9:_-]{1,128}$/.test(value)
export const validWorkspaceTime = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value > 0
const text = (value: unknown, max: number, required = false): value is string => typeof value === "string" && value.length <= max && (!required || Boolean(value.trim())) && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)
const group = (value: unknown) => value === null || validWorkspaceId(value)
export function safeWorkspaceSource(value: unknown): value is string {
  if (!text(value, 2000) || /[\r\n\\]/.test(value)) return false
  if (!value) return true
  try {
    const url = new URL(value)
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password
  } catch { return false }
}
export function safeWorkspaceReader(value: unknown): value is string {
  if (!text(value, 10000, true) || !value.startsWith("/") || value.startsWith("//") || /[\\\r\n]/.test(value)) return false
  const url = new URL(value, "https://agora.invalid")
  return url.origin === "https://agora.invalid" && /^\/(?:design-demo\/)?papers\/[A-Za-z0-9_-]+\/read$/.test(url.pathname) && !url.hash
}
export function validConstraints(value: unknown): value is ResearchConstraints {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const c = value as ResearchConstraints
  const allowed = ["recentYear", "hasCode", "paperType", "language", "license", "recentlyActive", "localRunnable"]
  return Object.keys(c).every(key => allowed.includes(key))
    && [c.recentYear, c.hasCode, c.recentlyActive, c.localRunnable].every(value => value === undefined || typeof value === "boolean")
    && (c.paperType === undefined || c.paperType === "survey")
    && (c.language === undefined || ["python", "typescript", "javascript", "rust", "go"].includes(c.language))
    && (c.license === undefined || ["MIT", "Apache-2.0", "BSD-3-Clause"].includes(c.license))
}
export function validMaterial(value: unknown): value is ResearchMaterial {
  if (!value || typeof value !== "object") return false
  const m = value as ResearchMaterial
  return validWorkspaceId(m.id) && group(m.groupId) && ["paper", "project", "discussion", "note", "pdf"].includes(m.kind)
    && text(m.title, 500, true) && safeWorkspaceSource(m.url) && (m.kind === "note" || Boolean(m.url) || Boolean(m.readerHref))
    && text(m.notes, 10000) && (m.referenceId === undefined || text(m.referenceId, 200, true))
    && (m.readerHref === undefined || safeWorkspaceReader(m.readerHref))
    && validWorkspaceTime(m.createdAt) && validWorkspaceTime(m.updatedAt) && m.updatedAt >= m.createdAt
}
export function validReportDraft(value: unknown): value is ResearchReportDraft {
  if (!value || typeof value !== "object") return false
  const d = value as ResearchReportDraft
  return validWorkspaceId(d.id) && (d.groupId === undefined || group(d.groupId)) && text(d.question, 2000) && text(d.title, 2000, true)
    && text(d.scope, 10000) && text(d.notes, 30000) && validWorkspaceTime(d.updatedAt) && Array.isArray(d.materials) && d.materials.length <= 100
    && d.materials.every(m => m && validWorkspaceId(m.id) && text(m.title, 500) && safeWorkspaceSource(m.url) && text(m.notes, 10000))
    && new Set(d.materials.map(m => m.id)).size === d.materials.length
}
export function validPrompt(value: unknown): value is ResearchPrompt {
  if (!value || typeof value !== "object") return false
  const p = value as ResearchPrompt
  return validWorkspaceId(p.id) && text(p.name, 80, true) && text(p.question, 2000, true) && isResearchMode(p.mode) && validConstraints(p.constraints) && validWorkspaceTime(p.updatedAt)
}
export function validComposerDraft(value: unknown): value is ResearchComposerDraft {
  if (!value || typeof value !== "object") return false
  const d = value as ResearchComposerDraft
  return text(d.question, 2000) && isResearchMode(d.mode) && validConstraints(d.constraints) && group(d.groupId) && validWorkspaceTime(d.updatedAt)
    && (d.materialIds === undefined || (Array.isArray(d.materialIds) && d.materialIds.length <= 50 && d.materialIds.every(validWorkspaceId) && new Set(d.materialIds).size === d.materialIds.length))
    && (d.submittedSessionId === undefined || validWorkspaceId(d.submittedSessionId))
}
export function validActivity(value: unknown): value is ResearchActivity {
  if (!value || typeof value !== "object") return false
  const a = value as ResearchActivity
  if (!text(a.title, 500, true) || !validWorkspaceTime(a.updatedAt)) return false
  if (a.kind === "report") return validWorkspaceId(a.draftId)
  if (a.kind !== "reader") return false
  return text(a.paperId, 200, true) && safeWorkspaceReader(a.href)
    && (a.sectionId === undefined || text(a.sectionId, 200, true)) && (a.sectionTitle === undefined || text(a.sectionTitle, 500))
    && (a.pdfPage === undefined || (Number.isInteger(a.pdfPage) && a.pdfPage > 0 && a.pdfPage <= 10000))
}
