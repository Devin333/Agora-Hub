import { researchQuestionHref, researchSearchQuery, type ResearchModule } from "./entry"
import type { ResearchConstraints } from "./workspace-items"

const patterns = {
  recentYear: /近一(?:年|年内)|最近一年|过去一年|\bpast year\b|\blast year\b/gi,
  hasCode: /有代码|附(?:带)?代码|带代码|\bwith code\b/gi,
  recentlyActive: /最近活跃|近期活跃|\brecently active\b/gi,
}

export function inferResearchConstraints(question: string, module: ResearchModule): ResearchConstraints {
  const matches = (pattern: RegExp) => new RegExp(pattern.source, pattern.flags).test(question)
  if (module === "papers") return { ...(matches(patterns.recentYear) ? { recentYear: true } : {}), ...(matches(patterns.hasCode) ? { hasCode: true } : {}) }
  if (module === "projects") return { ...(matches(patterns.recentlyActive) ? { recentlyActive: true } : {}), ...(question.match(/\bpython\b/i) ? { language: "python" as const } : {}) }
  return {}
}

export function researchEntryHref(module: ResearchModule, question: string, sessionId: string, options: { constraints?: ResearchConstraints; groupId?: string | null; materialIds?: string[] } = {}, now = new Date()): string {
  const url = new URL(researchQuestionHref(module, question, sessionId), "https://agora.invalid")
  const constraints = options.constraints ?? {}
  let query = question
  if (module === "papers") {
    // Recognized condition words are removed even when the user disables their chip.
    query = query.replace(patterns.recentYear, " ").replace(patterns.hasCode, " ")
    if (constraints.recentYear) {
      const from = new Date(now); from.setUTCFullYear(from.getUTCFullYear() - 1)
      url.searchParams.set("from", from.toISOString().slice(0, 10)); url.searchParams.set("to", now.toISOString().slice(0, 10))
    }
    if (constraints.hasCode) url.searchParams.set("has", "code")
    if (constraints.paperType) url.searchParams.set("paperType", constraints.paperType)
  }
  if (module === "projects") {
    query = query.replace(patterns.recentlyActive, " ")
    if (constraints.language) url.searchParams.set("language", constraints.language)
    if (constraints.recentlyActive) { url.searchParams.set("period", "monthly"); url.searchParams.set("sort", "activity") }
    if (constraints.license) url.searchParams.set("license", constraints.license)
    if (constraints.localRunnable) url.searchParams.set("localRunnable", "1")
  }
  const normalized = researchSearchQuery(query.replace(/\s+/g, " ").trim()).replace(/^的\s*|\s*的$/g, "").trim()
  url.searchParams.set("q", normalized === "论文" || normalized === "项目" ? "" : normalized)
  if (options.groupId) url.searchParams.set("researchGroup", options.groupId)
  for (const id of new Set(options.materialIds ?? [])) url.searchParams.append("material", id)
  return url.pathname + url.search
}
