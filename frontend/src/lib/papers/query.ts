import { paperMatchesFeatureFilters, parsePaperFeatureFilters, type PaperFeatureFilter } from "@/lib/papers/filters"
import { sortPapers } from "@/lib/papers/format"
import type { MethodRef, Paper, PaperPeriod, PaperSort, TaskRef } from "@/lib/papers/types"

export type PaperQuery = {
  q?: string
  period?: PaperPeriod
  sort?: PaperSort
  topic?: string
  from?: string
  to?: string
  task?: string
  method?: string
  has?: PaperFeatureFilter[] | string
}

export type NormalizedPaperQuery = {
  q: string
  terms: string[]
  period: PaperPeriod
  sort: PaperSort
  topic?: string
  from?: string
  to?: string
  task?: string
  method?: string
  has: PaperFeatureFilter[]
}

export type PaperQueryResult = {
  papers: Paper[]
  query: NormalizedPaperQuery
  earliestPublishedAt?: string
  latestPublishedAt?: string
}

type DateBoundary = "from" | "to"

/**
 * Applies the public paper catalogue's deterministic filters and ordering.
 * Search is deliberately lexical: every whitespace-delimited term must occur
 * in at least one indexed field. It does not claim semantic or AI retrieval.
 */
export function queryPapers(papers: Paper[], input: PaperQuery = {}, now: Date | string = new Date()): PaperQueryResult {
  const query = normalizePaperQuery(input)
  const publicPapers = papers.filter((paper) => paper.isPublished === true)
  const corpusRange = publishedDateRange(publicPapers)
  const periodStart = periodStartDate(query.period, now)
  const from = parseDateBoundary(query.from, "from")
  const to = parseDateBoundary(query.to, "to")

  const filtered = publicPapers.filter((paper) => {
    const publishedAt = validTimestamp(paper.publishedAt)
    if ((periodStart !== undefined || from !== undefined || to !== undefined) && publishedAt === undefined) {
      return false
    }
    if (periodStart !== undefined && publishedAt! < periodStart) {
      return false
    }
    if (from !== undefined && publishedAt! < from) {
      return false
    }
    if (to !== undefined && publishedAt! > to) {
      return false
    }
    if (query.topic && !paper.tags.some((tag) => lower(tag.trim()) === lower(query.topic!))) {
      return false
    }
    if (query.task && !matchesRef(query.task, paper.taskRefs, "task")) {
      return false
    }
    if (query.method && !matchesRef(query.method, paper.methodRefs, "method")) {
      return false
    }
    if (!paperMatchesFeatureFilters(paper, query.has)) {
      return false
    }
    return query.terms.every((term) => searchableText(paper).includes(term))
  })

  const sorted = query.sort === "relevance" && query.terms.length
    ? sortByRelevance(filtered, query.terms)
    : sortPapers(filtered, query.sort === "relevance" ? "trending" : query.sort)

  return { papers: sorted, query, ...corpusRange }
}

export function normalizePaperQuery(input: PaperQuery = {}): NormalizedPaperQuery {
  const q = input.q?.trim().replace(/\s+/g, " ") ?? ""
  const terms = uniqueStrings(q.split(/\s+/).map(lower).filter(Boolean))
  const period = isPaperPeriod(input.period) ? input.period : "all"
  const requestedSort = isPaperSort(input.sort) ? input.sort : q ? "relevance" : "trending"

  return {
    q,
    terms,
    period,
    sort: requestedSort === "relevance" && !terms.length ? "trending" : requestedSort,
    topic: clean(input.topic),
    from: normalizedDate(input.from),
    to: normalizedDate(input.to),
    task: clean(input.task),
    method: clean(input.method),
    has: parsePaperFeatureFilters(input.has)
  }
}

function sortByRelevance(papers: Paper[], terms: string[]) {
  return [...papers].sort((left, right) => {
    const scoreDifference = relevanceScore(right, terms) - relevanceScore(left, terms)
    if (scoreDifference) {
      return scoreDifference
    }
    const trendDifference = trendScore(right) - trendScore(left)
    if (trendDifference) {
      return trendDifference
    }
    const dateDifference = (validTimestamp(right.publishedAt) ?? 0) - (validTimestamp(left.publishedAt) ?? 0)
    return dateDifference || left.id.localeCompare(right.id)
  })
}

function relevanceScore(paper: Paper, terms: string[]) {
  const fields = [
    { value: [paper.title, paper.titleZh].filter(Boolean).join(" "), weight: 16 },
    { value: paper.tags.join(" "), weight: 10 },
    { value: refText(paper.taskRefs), weight: 8 },
    { value: refText(paper.methodRefs), weight: 7 },
    { value: [paper.abstractSnippet, paper.abstractSnippetZh].filter(Boolean).join(" "), weight: 4 },
    { value: paper.authors.join(" "), weight: 2 }
  ]

  return fields.reduce((total, field) => {
    const value = lower(field.value)
    return total + terms.reduce((score, term) => score + (value.includes(term) ? field.weight : 0), 0)
  }, 0)
}

function searchableText(paper: Paper) {
  return lower([
    paper.title,
    paper.titleZh,
    paper.abstractSnippet,
    paper.abstractSnippetZh,
    paper.authors.join(" "),
    paper.tags.join(" "),
    refText(paper.taskRefs),
    refText(paper.methodRefs)
  ].filter(Boolean).join(" "))
}

function refText(refs: Array<TaskRef | MethodRef>) {
  return refs.map((ref) => {
    const category = "area" in ref ? ref.area ?? "" : "group" in ref ? ref.group ?? "" : ""
    return `${ref.slug} ${ref.name} ${ref.nameZh ?? ""} ${category}`
  }).join(" ")
}

function matchesRef(value: string, refs: Array<TaskRef | MethodRef>, kind: "task" | "method") {
  const normalized = lower(value)
  const canonical = canonicalRefSlug(value, kind)
  return refs.some((ref) => (
    canonicalRefSlug(ref.slug, kind) === canonical ||
    lower(ref.slug) === normalized ||
    lower(ref.name) === normalized ||
    lower(ref.nameZh ?? "") === normalized
  ))
}

function canonicalRefSlug(value: string, kind: "task" | "method") {
  const normalized = lower(value)
  if (kind === "task") {
    return normalized === "task-agent-task-completion" ? "agent-task-completion"
      : normalized === "task-language-models" ? "language-models"
      : normalized
  }
  return normalized === "method-language-models" ? "language-models"
    : normalized === "method-large-language-models" ? "large-language-model"
    : normalized
}

function periodStartDate(period: PaperPeriod, now: Date | string) {
  const days = period === "daily" ? 1 : period === "weekly" ? 7 : period === "monthly" ? 30 : 0
  const nowTimestamp = validTimestamp(now instanceof Date ? now.toISOString() : now)
  return days && nowTimestamp !== undefined ? nowTimestamp - days * 24 * 60 * 60 * 1000 : undefined
}

function parseDateBoundary(value: string | undefined, boundary: DateBoundary) {
  if (!value) {
    return undefined
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    if (!isCalendarDate(value)) {
      return undefined
    }
    return validTimestamp(`${value}T${boundary === "from" ? "00:00:00.000" : "23:59:59.999"}Z`)
  }
  return validTimestamp(value)
}

function isCalendarDate(value: string) {
  const [year, month, day] = value.split("-").map(Number)
  const parsed = new Date(Date.UTC(year, month - 1, day))
  return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day
}

function normalizedDate(value: string | undefined) {
  const cleaned = clean(value)
  return cleaned && parseDateBoundary(cleaned, "from") !== undefined ? cleaned : undefined
}

function publishedDateRange(papers: Paper[]) {
  const timestamps = papers
    .map((paper) => ({ timestamp: validTimestamp(paper.publishedAt), value: paper.publishedAt }))
    .filter((item): item is { timestamp: number; value: string } => item.timestamp !== undefined)
    .sort((left, right) => left.timestamp - right.timestamp)

  return {
    earliestPublishedAt: timestamps[0]?.value,
    latestPublishedAt: timestamps.at(-1)?.value
  }
}

function trendScore(paper: Paper) {
  return (paper.githubStars ?? 0) + (paper.githubMomentum ?? 0) * 100 + (paper.citationCount ?? 0) * 2
}

function validTimestamp(value: string) {
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : undefined
}

function clean(value: string | undefined) {
  const result = value?.trim()
  return result || undefined
}

function lower(value: string) {
  return value.toLocaleLowerCase()
}

function uniqueStrings(values: string[]) {
  return Array.from(new Set(values))
}

function isPaperPeriod(value: PaperPeriod | undefined): value is PaperPeriod {
  return value === "daily" || value === "weekly" || value === "monthly" || value === "all"
}

function isPaperSort(value: PaperSort | undefined): value is PaperSort {
  return value === "trending" || value === "relevance" || value === "newest" || value === "most_cited"
}
