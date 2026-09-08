import type {
  EvidenceRef,
  ProjectCategory,
  ProjectCategoryAlias,
  ProjectCategoryRef,
  ProjectDataState,
  ProjectDetailResult,
  ProjectItem,
  ProjectLanguage,
  ProjectListOptions,
  ProjectListParams,
  ProjectListResult,
  ProjectMaturity,
  ProjectMetric,
  ProjectPeriod,
  ProjectScores,
  ProjectSort,
  ProjectSource,
  RelatedCommunityRef,
  RelatedNewsRef,
  RelatedPaperRef,
} from "@/types/projects"

export const PROJECT_CATEGORIES: ProjectCategory[] = [
  "agent_framework",
  "rag",
  "llm_infra",
  "inference",
  "evaluation",
  "coding",
  "multimodal",
  "data",
  "memory",
  "workflow",
]

export const PROJECT_MATURITIES: ProjectMaturity[] = ["new", "rising", "active", "mature", "dormant", "experimental"]

export const PROJECT_SOURCES: ProjectSource[] = ["github", "huggingface", "paper", "manual"]

export const PROJECT_LANGUAGES: ProjectLanguage[] = ["python", "typescript", "rust", "go", "other"]

export const PROJECT_SORTS: ProjectSort[] = ["trending", "newest", "stars", "activity", "growth", "quality"]

export const PROJECT_CATEGORY_LABELS: Record<ProjectCategory, string> = {
  agent_framework: "Agent Framework",
  rag: "RAG",
  llm_infra: "LLM Infra",
  inference: "Inference",
  evaluation: "Evaluation",
  coding: "Coding",
  multimodal: "Multimodal",
  data: "Data",
  memory: "Memory",
  workflow: "Workflow",
}

export const PROJECT_MATURITY_LABELS: Record<ProjectMaturity, string> = {
  new: "New",
  rising: "Rising",
  active: "Active",
  mature: "Mature",
  dormant: "Dormant",
  experimental: "Experimental",
}

const DEFAULT_PAGE_SIZE = 24
const MAX_PAGE_SIZE = 100
const GITHUB_REPO_HOST = "github.com"
const FORBIDDEN_KEYS = new Set(["raw_payload", "raw_html", "raw_content", "full_text", "token", "secret", "api_key"])

type NormalizedProjectParams = Required<Pick<ProjectListParams, "sort" | "page" | "pageSize">> & ProjectListParams

type ProjectSourceKind = ProjectListResult["source"]

type ProjectBuildContext = {
  source: ProjectSourceKind
  sourceRunId?: string
  generatedAt?: string
}

type ProjectMappingResult = {
  items: ProjectItem[]
  generatedAt?: string
  notices: string[]
}

type GitHubRepo = {
  owner: string
  repo: string
  fullName: string
  url: string
}

export function normalizeProjectParams(params: ProjectListParams = {}): NormalizedProjectParams {
  const pageFromCursor = cursorToPage(params.cursor)
  const page = clampInteger(pageFromCursor ?? params.page, 1, Number.MAX_SAFE_INTEGER, 1)
  const pageSize = clampInteger(params.limit ?? params.pageSize, 1, MAX_PAGE_SIZE, DEFAULT_PAGE_SIZE)
  const sort = normalizeSort(params.sort)
  return {
    ...params,
    sort,
    page,
    pageSize,
  }
}

export function buildProjectListResult(payload: unknown, params: ProjectListParams, context: ProjectBuildContext): ProjectListResult {
  const normalized = normalizeProjectParams(params)
  if (context.source === "none") {
    return emptyProjectList(normalized, context, "empty", ["No project_radar backend or local artifact data is available."])
  }

  const mapped = mapProjectPayload(payload)
  const allItems = sortProjects(mapped.items, normalized.sort)
  const filteredItems = sortProjects(filterProjects(allItems, normalized), normalized.sort)
  const total = filteredItems.length
  const start = (normalized.page - 1) * normalized.pageSize
  const items = filteredItems.slice(start, start + normalized.pageSize)
  const dataState = resolveDataState(Boolean(payload), allItems.length, mapped.notices)
  const hasNext = start + normalized.pageSize < total

  return {
    items,
    allItems,
    allFiltered: filteredItems,
    metrics: buildMetrics(allItems),
    options: buildOptions(allItems),
    page: {
      page: normalized.page,
      pageSize: normalized.pageSize,
      total,
      hasNext,
      nextCursor: hasNext ? String(normalized.page + 1) : null,
    },
    dataState,
    source: context.source,
    sourceRunId: context.sourceRunId,
    generatedAt: context.generatedAt ?? mapped.generatedAt,
    notices: mapped.notices,
  }
}

export function buildProjectDetailResult(
  payload: unknown,
  slug: string,
  context: Omit<ProjectBuildContext, "source"> & { source: Exclude<ProjectSourceKind, "none"> }
): ProjectDetailResult | null {
  const mapped = mapProjectPayload(payload)
  const normalizedSlug = slug.trim().toLowerCase()
  const project = mapped.items.find((item) => matchesProjectSlug(item, normalizedSlug))
  if (!project) {
    return null
  }
  return {
    project,
    dataState: resolveDataState(Boolean(payload), mapped.items.length, mapped.notices),
    source: context.source,
    sourceRunId: context.sourceRunId,
    generatedAt: context.generatedAt ?? mapped.generatedAt,
    notices: mapped.notices,
  }
}

export function mapProjectPayload(payload: unknown): ProjectMappingResult {
  const root = unwrapPayload(payload)
  const rootRecord = recordValue(root)
  const generatedAt = firstStringFrom(rootRecord, ["generated_at", "generatedAt", "created_at", "createdAt"])
  const candidates = projectCandidates(root)
  const detailPages = detailPageCandidates(root)
  const items: ProjectItem[] = []
  const notices: string[] = []
  const seen = new Map<string, ProjectItem>()

  for (const candidate of candidates) {
    const item = mapProjectCandidate(candidate, detailPages)
    if (!item) {
      notices.push("Skipped a project_radar record without a valid public project or source URL.")
      continue
    }
    const key = (item.fullName || item.name || item.repoUrl).toLowerCase()
    const existing = seen.get(key)
    seen.set(key, existing ? mergeProjects(existing, item) : item)
  }

  for (const item of seen.values()) {
    items.push(item)
  }

  if (candidates.length > 0 && items.length === 0) {
    notices.push("A project_radar artifact exists, but no public GitHub project records could be displayed.")
  }

  return {
    items,
    generatedAt,
    notices: uniqueStrings(notices),
  }
}

export function filterProjects(items: ProjectItem[], params: ProjectListParams): ProjectItem[] {
  const q = params.q?.trim().toLowerCase()
  const category = normalizeCategory(params.category)
  const topic = params.topic?.trim().toLowerCase()
  const source = normalizeSource(params.source)
  const language = normalizeLanguage(params.language)
  const license = params.license
  const maturity = normalizeMaturity(params.maturity)
  const period = normalizePeriod(params.period)

  return items.filter((item) => {
    if (q && !searchText(item).includes(q)) return false
    if (category && !item.categories.includes(category)) return false
    if (topic && !projectTopicText(item).includes(topic)) return false
    if (source && !projectSources(item).includes(source)) return false
    if (language && normalizeLanguage(item.language) !== language) return false
    if (license && item.license !== license) return false
    if (maturity && item.maturity !== maturity) return false
    if (period && period !== "all" && !matchesPeriod(item, period)) return false
    return true
  })
}

export function sortProjects(items: ProjectItem[], sort: ProjectSort = "trending"): ProjectItem[] {
  return [...items].sort((left, right) => compareProjects(left, right, normalizeSort(sort)))
}

export function normalizeCategory(value: unknown): ProjectCategory | undefined {
  const text = stringValue(value)?.toLowerCase().replace(/\s+/g, "_").replace(/-/g, "_")
  if (!text) return undefined
  const aliases: Record<string, ProjectCategory> = {
    agent: "agent_framework",
    agents: "agent_framework",
    agent_framework: "agent_framework",
    framework: "agent_framework",
    frameworks: "agent_framework",
    rag: "rag",
    retrieval: "rag",
    llm: "llm_infra",
    llm_infra: "llm_infra",
    infra: "llm_infra",
    infrastructure: "llm_infra",
    inference: "inference",
    serving: "inference",
    runtime: "inference",
    evaluation: "evaluation",
    eval: "evaluation",
    benchmark: "evaluation",
    coding: "coding",
    code: "coding",
    devtool: "coding",
    developer_tool: "coding",
    multimodal: "multimodal",
    vision: "multimodal",
    audio: "multimodal",
    data: "data",
    dataset: "data",
    memory: "memory",
    workflow: "workflow",
    orchestration: "workflow",
  }
  return aliases[text] ?? (PROJECT_CATEGORIES.includes(text as ProjectCategory) ? (text as ProjectCategory) : undefined)
}

export function normalizeMaturity(value: unknown): ProjectMaturity | undefined {
  const text = stringValue(value)?.toLowerCase().replace(/\s+/g, "_").replace(/-/g, "_")
  return PROJECT_MATURITIES.includes(text as ProjectMaturity) ? (text as ProjectMaturity) : undefined
}

export function normalizeSource(value: unknown): ProjectSource | undefined {
  const text = stringValue(value)?.toLowerCase()
  return PROJECT_SOURCES.includes(text as ProjectSource) ? (text as ProjectSource) : undefined
}

export function normalizeLanguage(value: unknown): ProjectLanguage | undefined {
  const text = stringValue(value)?.trim().toLowerCase()
  if (!text) return undefined
  if (text === "ts" || text === "tsx" || text === "javascript" || text === "js") return "typescript"
  if (text === "py") return "python"
  if (text === "rs") return "rust"
  if (PROJECT_LANGUAGES.includes(text as ProjectLanguage)) return text as ProjectLanguage
  return "other"
}

export function normalizeGitHubRepoUrl(value: unknown): GitHubRepo | null {
  const text = stringValue(value)
  if (!text) return null
  const withProtocol = text.startsWith("github.com/") ? `https://${text}` : text

  try {
    const url = new URL(withProtocol)
    if (url.protocol !== "https:" || url.hostname.toLowerCase() !== GITHUB_REPO_HOST) {
      return null
    }
    const [owner, repoPart] = url.pathname.split("/").filter(Boolean)
    if (!owner || !repoPart) return null
    const repo = repoPart.replace(/\.git$/i, "")
    if (!repo || owner === "topics" || owner === "orgs") return null
    return {
      owner,
      repo,
      fullName: `${owner}/${repo}`,
      url: `https://${GITHUB_REPO_HOST}/${owner}/${repo}`,
    }
  } catch {
    return null
  }
}

function mapProjectCandidate(raw: Record<string, unknown>, detailPages: Array<Record<string, unknown>>): ProjectItem | null {
  const repo = findRepo(raw)
  if (!repo) return null

  const detail = findDetailPage(raw, detailPages)
  const metadata = recordValue(raw.metadata)
  const rankingFeatures = recordValue(raw.ranking_features) ?? recordValue(raw.rankingFeatures)
  const score = recordValue(raw.score)
  const quality = recordValue(raw.quality)
  const sourceRefs = mapEvidenceRefs([
    ...arrayValue(raw.sourceRefs),
    ...arrayValue(raw.source_refs),
    ...arrayValue(raw.evidence_refs),
    ...arrayValue(recordValue(raw.provenance)?.source_refs),
    ...arrayValue(recordValue(raw.provenance)?.evidence_refs),
  ])
  const categoryRefs = categoryRefsFromRaw(raw)
  const categories = uniqueBy(categoryRefs.map((ref) => ref.category), (category) => category)
  const rawTags = uniqueStrings([
    ...stringArray(raw.tags),
    ...stringArray(raw.topics),
    ...badgeLabels(raw.badges),
    ...objectRefLabels(raw.related_refs),
    ...objectRefLabels(raw.relation_refs),
  ]).filter((tag) => !["project_radar", "paper_radar", "ai_news", "community_pulse"].includes(tag.toLowerCase()))
  const topics = uniqueStrings([...stringArray(raw.topics), ...rawTags, ...categoryRefs.map((category) => category.label)])
  const createdAt = firstStringFrom(raw, ["createdAt", "created_at", "repo_created_at", "repoCreatedAt"])
  const updatedAt = firstStringFrom(raw, ["updatedAt", "updated_at", "last_updated_at", "lastUpdatedAt"])
  const pushedAt = firstStringFrom(raw, ["pushedAt", "pushed_at", "lastPushedAt", "last_pushed_at"])
  const firstSeenAt = firstStringFrom(raw, ["firstSeenAt", "first_seen_at", "published_at", "publishedAt", "generated_at", "generatedAt"])
  const stars = firstNumberFrom(raw, ["stars", "stargazers_count", "stargazerCount", "githubStars"]) ?? metricNumber(raw.metrics, "stars")
  const forks = firstNumberFrom(raw, ["forks", "forks_count", "forkCount"]) ?? metricNumber(raw.metrics, "forks")
  const watchers = firstNumberFrom(raw, ["watchers", "watchers_count", "subscribers_count"]) ?? metricNumber(raw.metrics, "watchers")
  const openIssues = firstNumberFrom(raw, ["openIssues", "open_issues", "openIssuesCount", "open_issues_count"])
  const starGrowth24h = firstNumberFrom(raw, ["starGrowth24h", "star_growth_24h", "stars_24h", "growth24h"])
  const starGrowth7d = firstNumberFrom(raw, ["starGrowth7d", "star_growth_7d", "stars_7d", "growth7d"])
  const relatedPapers = relatedPaperRefs(raw)
  const relatedNews = relatedNewsRefs(raw, sourceRefs)
  const relatedCommunityTopics = relatedCommunityRefs(raw)
  const evidenceCount = uniqueBy(
    [...relatedPapers, ...relatedNews, ...relatedCommunityTopics, ...sourceRefs],
    (ref) => ("url" in ref ? ref.url : undefined) ?? ref.id ?? ref.title ?? ""
  ).length
  const scores = buildScores(raw, rankingFeatures, score, quality, {
    starGrowth7d,
    starGrowth24h,
    stars,
    forks,
    watchers,
    openIssues,
    evidenceCount,
    updatedAt: pushedAt ?? updatedAt,
  })
  const maturity = normalizeMaturity(firstStringFrom(raw, ["maturity", "project_maturity", "stage"])) ?? deriveMaturity({
    text: [...rawTags, ...topics, ...categoryRefs.map((ref) => ref.label)].join(" "),
    createdAt,
    firstSeenAt,
    updatedAt: pushedAt ?? updatedAt,
    stars,
    starGrowth7d,
    starGrowth24h,
    scores,
  })

  const project: ProjectItem = {
    id: firstStringFrom(raw, ["id", "project_id", "projectId", "card_id", "cardId", "repo_id"]) ?? repo.url,
    slug: slugify(repo.fullName),
    name: projectName(raw, repo),
    fullName: firstStringFrom(raw, ["fullName", "full_name", "repo_full_name", "repoFullName"]) ?? repo.fullName,
    description: descriptionText(raw, detail) ?? repo.fullName,
    repoUrl: repo.url,
    homepageUrl: normalizeHttpsUrl(firstStringFrom(raw, ["homepageUrl", "homepage_url", "homepage", "website"])),
    owner: firstStringFrom(raw, ["owner", "repo_owner", "repoOwner"]) ?? repo.owner,
    language: firstStringFrom(raw, ["language", "primary_language", "primaryLanguage"]),
    license: licenseText(raw),
    stars,
    forks,
    watchers,
    openIssues,
    starGrowth24h,
    starGrowth7d,
    projectMomentum: scores.trendScore,
    qualityScore: scores.qualityScore,
    scores,
    categoryRefs,
    categories,
    tags: rawTags,
    topics,
    maturity,
    createdAt,
    updatedAt: updatedAt ?? pushedAt,
    pushedAt,
    lastPushedAt: pushedAt ?? updatedAt,
    firstSeenAt,
    sourceRefs,
    relatedPapers,
    relatedNews,
    relatedCommunityTopics,
    relationCounts: {
      papers: relatedPapers.length,
      news: relatedNews.length,
      community: relatedCommunityTopics.length,
    },
    problemSolved: firstStringFrom(raw, ["problemSolved", "problem_solved", "problem", "use_case"]) ?? sectionContent(detail, "problem"),
    whyItMatters:
      firstStringFrom(raw, ["whyItMatters", "why_it_matters", "impact", "ranking_reason"]) ??
      firstStringFrom(metadata, ["why_it_matters"]) ??
      sectionContent(detail, "why"),
    sources: projectSourcesFromRefs(repo, raw, sourceRefs),
  }

  return stripUndefined(project)
}

function buildScores(
  raw: Record<string, unknown>,
  rankingFeatures: Record<string, unknown> | undefined,
  score: Record<string, unknown> | undefined,
  quality: Record<string, unknown> | undefined,
  derived: {
    starGrowth7d?: number
    starGrowth24h?: number
    stars?: number
    forks?: number
    watchers?: number
    openIssues?: number
    evidenceCount: number
    updatedAt?: string
  }
): ProjectScores {
  const starVelocityScore =
    firstNumberFrom(raw, ["starVelocityScore", "star_velocity_score"]) ??
    numberValue(rankingFeatures?.star_velocity) ??
    numberValue(rankingFeatures?.starVelocity) ??
    derived.starGrowth7d ??
    derived.starGrowth24h
  const activityScore =
    firstNumberFrom(raw, ["activityScore", "activity_score"]) ??
    numberValue(rankingFeatures?.activity) ??
    numberValue(rankingFeatures?.activity_score) ??
    activityFromFields(derived)
  const freshnessScore =
    firstNumberFrom(raw, ["freshnessScore", "freshness_score"]) ??
    numberValue(rankingFeatures?.freshness) ??
    freshnessFromDate(derived.updatedAt)
  const adoptionScore =
    firstNumberFrom(raw, ["adoptionScore", "adoption_score"]) ??
    numberValue(rankingFeatures?.adoption) ??
    adoptionFromFields(derived)
  const evidenceScore =
    firstNumberFrom(raw, ["evidenceScore", "evidence_score"]) ??
    numberValue(rankingFeatures?.evidence) ??
    derived.evidenceCount
  const qualityScore =
    firstNumberFrom(raw, ["qualityScore", "quality_score"]) ??
    numberValue(quality?.score) ??
    numberValue(rankingFeatures?.weighted_score) ??
    numberValue(score?.value)
  const trendScore =
    firstNumberFrom(raw, ["projectMomentum", "project_momentum", "momentum", "trend_score", "trendScore"]) ??
    numberValue(rankingFeatures?.trend_score) ??
    numberValue(rankingFeatures?.weighted_score) ??
    weightedScore({
      starVelocityScore,
      activityScore,
      freshnessScore,
      evidenceScore,
      adoptionScore,
    })

  return stripUndefined({
    trendScore,
    starVelocityScore,
    freshnessScore,
    activityScore,
    adoptionScore,
    evidenceScore,
    qualityScore,
  })
}

function projectCandidates(payload: unknown): Array<Record<string, unknown>> {
  const root = unwrapPayload(payload)
  const arrays: unknown[] = []
  if (Array.isArray(root)) arrays.push(root)
  const record = recordValue(root)
  if (record) {
    for (const key of ["projects", "items", "radar_items", "radarItems", "cards"]) {
      arrays.push(record[key])
    }
    const boardOutput = recordValue(record.board_output) ?? recordValue(record.boardOutput)
    if (boardOutput) {
      for (const key of ["projects", "items", "radar_items", "radarItems", "cards"]) {
        arrays.push(boardOutput[key])
      }
    }
    const content = recordValue(record.content)
    if (content && content !== record) {
      arrays.push(...projectCandidates(content))
    }
  }
  return arrays.flatMap((value) => arrayValue(value).map(recordValue).filter(isRecord))
}

function detailPageCandidates(payload: unknown): Array<Record<string, unknown>> {
  const root = unwrapPayload(payload)
  const record = recordValue(root)
  if (!record) return []
  const boardOutput = recordValue(record.board_output) ?? recordValue(record.boardOutput)
  return [
    ...arrayValue(record.detail_pages),
    ...arrayValue(record.detailPages),
    ...arrayValue(boardOutput?.detail_pages),
    ...arrayValue(boardOutput?.detailPages),
  ].map(recordValue).filter(isRecord)
}

function findRepo(raw: Record<string, unknown>): GitHubRepo | null {
  for (const value of repoUrlCandidates(raw)) {
    const repo = normalizeGitHubRepoUrl(value)
    if (repo) return repo
  }
  for (const fullName of repoFullNameCandidates(raw)) {
    const repo = repoFromFullName(fullName)
    if (repo) return repo
  }
  const owner = firstStringFrom(raw, ["owner", "repo_owner", "repoOwner"])
  const repoName = firstStringFrom(raw, ["repo", "repo_name", "repoName"])
  if (owner && repoName) {
    return repoFromFullName(`${owner}/${repoName}`)
  }
  return projectRefFromPublicSource(raw)
}

function repoUrlCandidates(value: unknown, depth = 0): unknown[] {
  if (depth > 3) return []
  const record = recordValue(value)
  if (!record) return []
  const result: unknown[] = []
  for (const [key, item] of Object.entries(record)) {
    if (FORBIDDEN_KEYS.has(key.toLowerCase())) continue
    if (["repoUrl", "repo_url", "html_url", "github_url", "githubUrl", "source_url", "sourceUrl", "url"].includes(key)) {
      result.push(item)
    }
    if (["repo", "repository", "github", "metadata", "provenance"].includes(key)) {
      result.push(...repoUrlCandidates(item, depth + 1))
    }
  }
  return result
}

function repoFullNameCandidates(value: unknown, depth = 0): string[] {
  if (depth > 3) return []
  const record = recordValue(value)
  if (!record) return []
  const result: string[] = []
  for (const [key, item] of Object.entries(record)) {
    if (FORBIDDEN_KEYS.has(key.toLowerCase())) continue
    if (["repo_full_name", "repoFullName", "full_name", "fullName"].includes(key)) {
      const text = stringValue(item)
      if (text) result.push(text)
    }
    if (["repo", "repository", "github", "metadata"].includes(key)) {
      result.push(...repoFullNameCandidates(item, depth + 1))
    }
  }
  return result
}

function repoFromFullName(value: unknown): GitHubRepo | null {
  const text = stringValue(value)
  if (!text) return null
  const [owner, repo] = text.split("/").filter(Boolean)
  if (!owner || !repo) return null
  return normalizeGitHubRepoUrl(`https://github.com/${owner}/${repo.replace(/\.git$/i, "")}`)
}

function projectRefFromPublicSource(raw: Record<string, unknown>): GitHubRepo | null {
  const url = firstPublicSourceUrl(raw)
  const name = firstStringFrom(raw, ["name", "project_name", "projectName", "title"])
  if (!url || !name) return null

  try {
    const hostname = new URL(url).hostname.replace(/^www\./i, "")
    const projectName = slugify(name) || hostname
    return {
      owner: hostname,
      repo: projectName,
      fullName: name,
      url,
    }
  } catch {
    return null
  }
}

function firstPublicSourceUrl(raw: Record<string, unknown>): string | undefined {
  for (const value of [
    ...repoUrlCandidates(raw),
    ...arrayValue(raw.evidence_refs).flatMap((ref) => {
      const record = recordValue(ref)
      return record ? [record.url, record.source_url, record.sourceUrl] : []
    }),
    ...arrayValue(raw.sourceRefs).flatMap((ref) => {
      const record = recordValue(ref)
      return record ? [record.url, record.source_url, record.sourceUrl] : []
    }),
  ]) {
    const url = normalizeHttpsUrl(value)
    if (url) return url
  }
  return undefined
}

function findDetailPage(raw: Record<string, unknown>, pages: Array<Record<string, unknown>>): Record<string, unknown> | undefined {
  const objectRef = recordValue(raw.primary_object_ref) ?? recordValue(raw.primaryObjectRef)
  const objectId = stringValue(objectRef?.object_id) ?? stringValue(objectRef?.objectId)
  const signalId = stringValue(recordValue(raw.metadata)?.signal_id) ?? stringValue(recordValue(raw.metadata)?.signalId)
  const title = stringValue(raw.title)?.toLowerCase()

  return pages.find((page) => {
    const pageRef = recordValue(page.primary_object_ref) ?? recordValue(page.primaryObjectRef)
    return (
      (objectId && (stringValue(pageRef?.object_id) === objectId || stringValue(pageRef?.objectId) === objectId)) ||
      (signalId && stringValue(recordValue(page.metadata)?.signal_id) === signalId) ||
      (title && stringValue(page.title)?.toLowerCase() === title)
    )
  })
}

function projectName(raw: Record<string, unknown>, repo: GitHubRepo): string {
  const value = firstStringFrom(raw, ["name", "project_name", "projectName", "title"])
  if (!value) {
    return repo.repo
  }
  if (repo.url.includes("github.com") && (value.toLowerCase() === repo.owner.toLowerCase() || value.toLowerCase() === repo.fullName.toLowerCase())) {
    return repo.repo
  }
  return value
}

function descriptionText(raw: Record<string, unknown>, detail?: Record<string, unknown>): string | undefined {
  return (
    firstStringFrom(raw, ["description", "summary", "detailed_summary", "subtitle"]) ??
    firstStringFrom(detail, ["summary"]) ??
    sectionContent(detail, "summary")
  )
}

function licenseText(raw: Record<string, unknown>): string | undefined {
  const direct = firstStringFrom(raw, ["license"])
  if (direct) return direct
  const license = recordValue(raw.license)
  return firstStringFrom(license, ["name", "spdx_id", "spdxId", "key"])
}

function categoryRefsFromRaw(raw: Record<string, unknown>): ProjectCategoryRef[] {
  const refs = arrayValue(raw.categoryRefs).concat(arrayValue(raw.category_refs))
  const categories: ProjectCategoryRef[] = []

  for (const ref of refs.map(recordValue).filter(isRecord)) {
    const category = normalizeCategory(ref.category ?? ref.value ?? ref.id)
    if (!category) continue
    const confidence = numberValue(ref.confidence)
    categories.push(
      stripUndefined({
        category,
        label: stringValue(ref.label) ?? categoryLabel(category),
        confidence,
      })
    )
  }

  for (const value of [raw.category, raw.type, ...stringArray(raw.categories), ...stringArray(raw.topics), ...stringArray(raw.tags), ...badgeLabels(raw.badges)]) {
    const inferred = inferCategory(stringValue(value))
    if (inferred && !categories.some((ref) => ref.category === inferred)) {
      categories.push({ category: inferred, label: categoryLabel(inferred) })
    }
  }
  return categories
}

function inferCategory(value: string | undefined): ProjectCategory | undefined {
  const text = value?.toLowerCase()
  if (!text) return undefined
  if (text.includes("coding") || text.includes("code agent") || text.includes("ide") || text.includes("devtool")) return "coding"
  if (text.includes("agent")) return "agent_framework"
  if (text.includes("rag") || text.includes("retrieval")) return "rag"
  if (text.includes("llm infra") || text.includes("observability") || text.includes("routing") || text.includes("gateway")) return "llm_infra"
  if (text.includes("infer") || text.includes("serving") || text.includes("runtime") || text.includes("quant")) return "inference"
  if (text.includes("eval") || text.includes("benchmark")) return "evaluation"
  if (text.includes("multi") || text.includes("vision") || text.includes("audio") || text.includes("video")) return "multimodal"
  if (text.includes("data") || text.includes("dataset") || text.includes("synthetic")) return "data"
  if (text.includes("memory") || text.includes("knowledge graph")) return "memory"
  if (text.includes("workflow") || text.includes("orchestration") || text.includes("scheduler")) return "workflow"
  if (text.includes("framework") || text.includes("tool")) return "agent_framework"
  return normalizeCategory(text as ProjectCategoryAlias)
}

function mapEvidenceRefs(values: unknown[]): EvidenceRef[] {
  return values
    .map(recordValue)
    .filter(isRecord)
    .map((ref) =>
      stripUndefined({
        id: firstStringFrom(ref, ["id", "evidence_id", "external_id", "source_id"]),
        title: firstStringFrom(ref, ["title", "label"]),
        sourceName: firstStringFrom(ref, ["sourceName", "source_name"]),
        sourceType: firstStringFrom(ref, ["sourceType", "source_type"]),
        sourceUrl: firstStringFrom(ref, ["sourceUrl", "source_url"]),
        url: firstStringFrom(ref, ["url"]),
        collectedAt: firstStringFrom(ref, ["collectedAt", "collected_at", "capturedAt", "captured_at"]),
        publishedAt: firstStringFrom(ref, ["publishedAt", "published_at"]),
        reliability: firstStringFrom(ref, ["reliability"]),
        summary: firstStringFrom(ref, ["summary"]),
      })
    )
}

function relatedPaperRefs(raw: Record<string, unknown>): RelatedPaperRef[] {
  return mapRelatedRefs([...arrayValue(raw.relatedPapers), ...arrayValue(raw.related_papers), ...objectRefsByType(raw.related_refs, "paper")])
}

function relatedNewsRefs(raw: Record<string, unknown>, sourceRefs: EvidenceRef[]): RelatedNewsRef[] {
  const explicit = mapRelatedRefs([...arrayValue(raw.relatedNews), ...arrayValue(raw.related_news), ...objectRefsByType(raw.related_refs, "news")])
  const evidenceNews = sourceRefs
    .filter((ref) => ref.url || ref.sourceUrl)
    .map((ref) =>
      stripUndefined({
        id: ref.id,
        title: ref.title ?? ref.sourceName ?? ref.url ?? ref.sourceUrl ?? "Evidence",
        url: ref.url ?? ref.sourceUrl,
        sourceName: ref.sourceName,
        publishedAt: ref.publishedAt,
      })
    )
  return uniqueBy([...explicit, ...evidenceNews], (ref) => ref.url ?? ref.id ?? ref.title)
}

function relatedCommunityRefs(raw: Record<string, unknown>): RelatedCommunityRef[] {
  return mapRelatedRefs([
    ...arrayValue(raw.relatedCommunityTopics),
    ...arrayValue(raw.related_community_topics),
    ...arrayValue(raw.related_discussions),
    ...objectRefsByType(raw.related_refs, "community"),
    ...objectRefsByType(raw.related_refs, "discussion"),
  ])
}

type RelatedRef = {
  id?: string
  title: string
  url?: string
  summary?: string
  sourceName?: string
  publishedAt?: string
}

function mapRelatedRefs(values: unknown[]): RelatedRef[] {
  return values
    .map(recordValue)
    .filter(isRecord)
    .map((ref) =>
      stripUndefined({
        id: firstStringFrom(ref, ["id", "object_id", "objectId"]),
        title: firstStringFrom(ref, ["title", "label", "name"]) ?? "Related item",
        url: firstStringFrom(ref, ["url", "source_url", "sourceUrl"]),
        summary: firstStringFrom(ref, ["summary"]),
        sourceName: firstStringFrom(ref, ["sourceName", "source_name"]),
        publishedAt: firstStringFrom(ref, ["publishedAt", "published_at"]),
      })
    )
}

function objectRefsByType(value: unknown, type: string): unknown[] {
  return arrayValue(value).filter((item) => {
    const ref = recordValue(item)
    const objectType = stringValue(ref?.object_type ?? ref?.objectType)?.toLowerCase()
    return Boolean(objectType?.includes(type))
  })
}

function projectSourcesFromRefs(repo: GitHubRepo, raw: Record<string, unknown>, refs: EvidenceRef[]): ProjectSource[] {
  const sources = new Set<ProjectSource>(repo.url.includes("github.com") ? ["github"] : ["manual"])
  const text = [
    repo.url,
    firstStringFrom(raw, ["source", "source_type", "sourceType"]),
    ...refs.flatMap((ref) => [ref.sourceType, ref.url, ref.sourceUrl, ref.sourceName]),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase()

  if (text.includes("huggingface") || text.includes("hugging face")) sources.add("huggingface")
  if (text.includes("arxiv") || text.includes("paper") || text.includes("openreview")) sources.add("paper")
  if (text.includes("manual")) sources.add("manual")
  return [...sources]
}

function projectSources(item: ProjectItem): ProjectSource[] {
  const sources = new Set<ProjectSource>(item.sources?.length ? item.sources : ["github"])
  for (const ref of item.sourceRefs ?? []) {
    const text = `${ref.sourceType ?? ""} ${ref.sourceName ?? ""} ${ref.url ?? ""} ${ref.sourceUrl ?? ""}`.toLowerCase()
    if (text.includes("huggingface")) sources.add("huggingface")
    if (text.includes("arxiv") || text.includes("paper") || text.includes("openreview")) sources.add("paper")
    if (text.includes("manual")) sources.add("manual")
  }
  return [...sources]
}

function searchText(item: ProjectItem): string {
  return [
    item.name,
    item.fullName,
    item.description,
    item.owner,
    item.language,
    item.license,
    item.repoUrl,
    item.problemSolved,
    item.whyItMatters,
    item.maturity,
    ...item.tags,
    ...item.topics,
    ...item.categoryRefs.map((ref) => ref.label),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase()
}

function projectTopicText(item: ProjectItem): string {
  return [
    ...item.topics,
    ...item.tags,
    ...item.categoryRefs.map((ref) => ref.label),
    ...item.categories,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase()
}

function compareProjects(left: ProjectItem, right: ProjectItem, sort: ProjectSort): number {
  if (sort === "newest") return compareDate(right.firstSeenAt ?? right.createdAt ?? right.updatedAt, left.firstSeenAt ?? left.createdAt ?? left.updatedAt)
  if (sort === "stars") return compareNumber(right.stars, left.stars)
  if (sort === "growth") return compareNumber(right.starGrowth7d ?? right.starGrowth24h, left.starGrowth7d ?? left.starGrowth24h)
  if (sort === "quality") return compareNumber(right.qualityScore, left.qualityScore)
  if (sort === "activity") {
    return (
      compareNumber(right.scores.activityScore, left.scores.activityScore) ||
      compareDate(right.pushedAt ?? right.updatedAt ?? right.lastPushedAt, left.pushedAt ?? left.updatedAt ?? left.lastPushedAt) ||
      compareNumber(right.openIssues, left.openIssues)
    )
  }
  return (
    compareNumber(right.scores.trendScore ?? right.projectMomentum, left.scores.trendScore ?? left.projectMomentum) ||
    compareNumber(right.scores.starVelocityScore ?? right.starGrowth7d ?? right.starGrowth24h, left.scores.starVelocityScore ?? left.starGrowth7d ?? left.starGrowth24h) ||
    compareNumber(right.scores.evidenceScore, left.scores.evidenceScore) ||
    compareNumber(right.stars, left.stars)
  )
}

function buildMetrics(items: ProjectItem[]): ProjectMetric[] {
  const withStars = items.filter((item) => item.stars !== undefined)
  const growth = items.reduce((sum, item) => sum + (item.starGrowth7d ?? item.starGrowth24h ?? 0), 0)
  const active = items.filter((item) => item.maturity === "active" || item.maturity === "rising" || item.maturity === "new").length
  const evidence = items.reduce((sum, item) => sum + item.relationCounts.papers + item.relationCounts.news + item.relationCounts.community, 0)
  return [
    { label: "Projects", value: items.length },
    { label: "With stars", value: withStars.length },
    { label: "Star delta", value: growth },
    { label: "Active signals", value: active, hint: `${evidence} related references` },
  ]
}

function buildOptions(items: ProjectItem[]): ProjectListOptions {
  return {
    categories: PROJECT_CATEGORIES.map((category) => ({
      value: category,
      label: categoryLabel(category),
      count: items.filter((item) => item.categories.includes(category)).length,
    })).filter((option) => option.count > 0),
    sources: PROJECT_SOURCES.map((source) => ({
      value: source,
      label: sourceLabel(source),
      count: items.filter((item) => projectSources(item).includes(source)).length,
    })).filter((option) => option.count > 0),
    languages: PROJECT_LANGUAGES.map((language) => ({
      value: language,
      label: languageLabel(language),
      count: items.filter((item) => normalizeLanguage(item.language) === language).length,
    })).filter((option) => option.count > 0),
    topics: countOptions(items.flatMap((item) => item.topics).filter((topic) => topic.length <= 40)).slice(0, 24),
    maturity: PROJECT_MATURITIES.map((maturity) => ({
      value: maturity,
      label: maturityLabel(maturity),
      count: items.filter((item) => item.maturity === maturity).length,
    })).filter((option) => option.count > 0),
  }
}

function emptyProjectList(
  params: NormalizedProjectParams,
  context: ProjectBuildContext,
  dataState: ProjectDataState,
  notices: string[]
): ProjectListResult {
  return {
    items: [],
    allItems: [],
    allFiltered: [],
    metrics: buildMetrics([]),
    options: { categories: [], sources: [], languages: [], topics: [], maturity: [] },
    page: { page: params.page, pageSize: params.pageSize, total: 0, hasNext: false, nextCursor: null },
    dataState,
    source: context.source,
    sourceRunId: context.sourceRunId,
    generatedAt: context.generatedAt,
    notices,
  }
}

function resolveDataState(hasPayload: boolean, itemCount: number, notices: string[]): ProjectDataState {
  if (!hasPayload) return "empty"
  if (itemCount === 0) return "partial"
  return notices.length ? "partial" : "ready"
}

function categoryLabel(category: ProjectCategory): string {
  return PROJECT_CATEGORY_LABELS[category]
}

function maturityLabel(maturity: ProjectMaturity): string {
  return PROJECT_MATURITY_LABELS[maturity]
}

function sourceLabel(source: ProjectSource): string {
  const labels: Record<ProjectSource, string> = {
    github: "GitHub",
    huggingface: "Hugging Face",
    paper: "Paper",
    manual: "Manual",
  }
  return labels[source]
}

function languageLabel(language: ProjectLanguage): string {
  const labels: Record<ProjectLanguage, string> = {
    python: "Python",
    typescript: "TypeScript",
    rust: "Rust",
    go: "Go",
    other: "Other",
  }
  return labels[language]
}

function normalizeSort(value: unknown): ProjectSort {
  const text = stringValue(value)?.toLowerCase()
  if (text === "top" || text === "trend") return "trending"
  if (text === "new") return "newest"
  if (text === "active") return "activity"
  return PROJECT_SORTS.includes(text as ProjectSort) ? (text as ProjectSort) : "trending"
}

function normalizePeriod(value: unknown): ProjectPeriod | undefined {
  const text = stringValue(value)?.toLowerCase()
  if (text === "today") return "daily"
  if (text === "week") return "weekly"
  if (text === "month") return "monthly"
  if (text === "daily" || text === "weekly" || text === "monthly" || text === "all") return text
  return undefined
}

function matchesPeriod(item: ProjectItem, period: Exclude<ProjectPeriod, "all">): boolean {
  const dateValue = item.firstSeenAt ?? item.createdAt ?? item.updatedAt ?? item.pushedAt ?? item.lastPushedAt
  const time = dateValue ? Date.parse(dateValue) : Number.NaN
  if (!Number.isFinite(time)) return false
  return time >= Date.now() - periodDays(period) * 24 * 60 * 60 * 1000
}

function periodDays(period: Exclude<ProjectPeriod, "all">): number {
  if (period === "daily") return 1
  if (period === "weekly") return 7
  return 30
}

function deriveMaturity(input: {
  text: string
  createdAt?: string
  firstSeenAt?: string
  updatedAt?: string
  stars?: number
  starGrowth7d?: number
  starGrowth24h?: number
  scores: ProjectScores
}): ProjectMaturity | undefined {
  const text = input.text.toLowerCase()
  if (text.includes("experimental") || text.includes("demo") || text.includes("prototype")) return "experimental"
  const firstSeenTime = timeValue(input.firstSeenAt ?? input.createdAt)
  const updatedTime = timeValue(input.updatedAt)
  const ageDays = firstSeenTime === undefined ? undefined : (Date.now() - firstSeenTime) / (24 * 60 * 60 * 1000)
  const staleDays = updatedTime === undefined ? undefined : (Date.now() - updatedTime) / (24 * 60 * 60 * 1000)
  const starVelocity = input.starGrowth7d ?? input.starGrowth24h ?? input.scores.starVelocityScore

  if (ageDays !== undefined && ageDays >= 0 && ageDays <= 30) return "new"
  if (starVelocity !== undefined && starVelocity > 0) return "rising"
  if (staleDays !== undefined && staleDays <= 45) return "active"
  if (input.stars !== undefined && input.stars >= 1000 && (staleDays === undefined || staleDays <= 365)) return "mature"
  if (staleDays !== undefined && staleDays > 365) return "dormant"
  return undefined
}

function matchesProjectSlug(item: ProjectItem, value: string): boolean {
  return [
    item.slug,
    item.id,
    item.fullName,
    item.repoUrl,
    slugify(item.fullName),
    slugify(item.name),
  ]
    .map((candidate) => candidate.toLowerCase())
    .includes(value)
}

function sectionContent(detail: Record<string, unknown> | undefined, hint: string): string | undefined {
  const sections = arrayValue(detail?.sections).map(recordValue).filter(isRecord)
  const match = sections.find((section) => {
    const title = `${stringValue(section.title) ?? ""} ${stringValue(section.section_type) ?? ""}`.toLowerCase()
    return title.includes(hint)
  })
  return firstStringFrom(match, ["content", "summary"])
}

function metricNumber(value: unknown, label: string): number | undefined {
  const metrics = arrayValue(value).map(recordValue).filter(isRecord)
  const metric = metrics.find((item) => stringValue(item.label)?.toLowerCase().includes(label.toLowerCase()))
  return numberValue(metric?.value)
}

function badgeLabels(value: unknown): string[] {
  return arrayValue(value)
    .map(recordValue)
    .filter(isRecord)
    .map((badge) => stringValue(badge.label))
    .filter(isString)
}

function objectRefLabels(value: unknown): string[] {
  return arrayValue(value)
    .map(recordValue)
    .filter(isRecord)
    .map((ref) => stringValue(ref.label))
    .filter(isString)
}

function stringArray(value: unknown): string[] {
  return arrayValue(value).map(stringValue).filter(isString)
}

function firstStringFrom(record: Record<string, unknown> | undefined, keys: string[]): string | undefined {
  if (!record) return undefined
  for (const key of keys) {
    const value = stringValue(record[key])
    if (value) return value
  }
  return undefined
}

function firstNumberFrom(record: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = numberValue(record[key])
    if (value !== undefined) return value
  }
  return undefined
}

function normalizeHttpsUrl(value: unknown): string | undefined {
  const text = stringValue(value)
  if (!text) return undefined
  try {
    const url = new URL(text)
    if (url.protocol !== "https:") return undefined
    return url.toString()
  } catch {
    return undefined
  }
}

function unwrapPayload(payload: unknown): unknown {
  const record = recordValue(payload)
  if (!record) return payload
  const content = recordValue(record.content)
  return content ?? payload
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined
}

function isRecord(value: Record<string, unknown> | undefined): value is Record<string, unknown> {
  return Boolean(value)
}

function stringValue(value: unknown): string | undefined {
  if (typeof value !== "string" && typeof value !== "number") return undefined
  const text = String(value).trim()
  return text ? text : undefined
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim()) {
    const number = Number(value)
    return Number.isFinite(number) ? number : undefined
  }
  return undefined
}

function isString(value: string | undefined): value is string {
  return Boolean(value)
}

function compareNumber(left: number | undefined, right: number | undefined): number {
  return (left ?? Number.NEGATIVE_INFINITY) - (right ?? Number.NEGATIVE_INFINITY)
}

function compareDate(left: string | undefined, right: string | undefined): number {
  const leftTime = left ? Date.parse(left) : Number.NEGATIVE_INFINITY
  const rightTime = right ? Date.parse(right) : Number.NEGATIVE_INFINITY
  return (Number.isFinite(leftTime) ? leftTime : Number.NEGATIVE_INFINITY) - (Number.isFinite(rightTime) ? rightTime : Number.NEGATIVE_INFINITY)
}

function timeValue(value: string | undefined): number | undefined {
  if (!value) return undefined
  const time = Date.parse(value)
  return Number.isFinite(time) ? time : undefined
}

function freshnessFromDate(value: string | undefined): number | undefined {
  const time = timeValue(value)
  if (time === undefined) return undefined
  const days = Math.max(0, (Date.now() - time) / (24 * 60 * 60 * 1000))
  if (days <= 7) return 100
  if (days <= 30) return 75
  if (days <= 90) return 50
  if (days <= 365) return 25
  return 5
}

function activityFromFields(value: { forks?: number; watchers?: number; openIssues?: number; updatedAt?: string }): number | undefined {
  const parts = [value.forks, value.watchers, value.openIssues, freshnessFromDate(value.updatedAt)].filter(isNumber)
  if (!parts.length) return undefined
  return parts.reduce((sum, item) => sum + item, 0)
}

function adoptionFromFields(value: { stars?: number; forks?: number; watchers?: number }): number | undefined {
  const parts = [value.stars, value.forks, value.watchers].filter(isNumber)
  if (!parts.length) return undefined
  return parts.reduce((sum, item) => sum + item, 0)
}

function weightedScore(value: {
  starVelocityScore?: number
  activityScore?: number
  freshnessScore?: number
  evidenceScore?: number
  adoptionScore?: number
}): number | undefined {
  const parts = [
    scaleScore(value.starVelocityScore, 0.35),
    scaleScore(value.activityScore, 0.2),
    scaleScore(value.freshnessScore, 0.15),
    scaleScore(value.evidenceScore, 0.15),
    scaleScore(value.adoptionScore, 0.15),
  ].filter(isNumber)
  if (!parts.length) return undefined
  return parts.reduce((sum, item) => sum + item, 0)
}

function scaleScore(value: number | undefined, weight: number): number | undefined {
  if (value === undefined) return undefined
  return value * weight
}

function isNumber(value: number | undefined): value is number {
  return value !== undefined && Number.isFinite(value)
}

function countOptions(values: string[]) {
  const counts = new Map<string, number>()
  for (const value of values.map((item) => item.trim()).filter(Boolean)) {
    counts.set(value, (counts.get(value) ?? 0) + 1)
  }
  return [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .map(([value, count]) => ({ value, label: value, count }))
}

function cursorToPage(value: string | undefined): number | undefined {
  if (!value) return undefined
  const number = Number(value)
  return Number.isInteger(number) && number > 0 ? number : undefined
}

function clampInteger(value: unknown, min: number, max: number, fallback: number): number {
  const number = typeof value === "number" ? value : Number(value)
  if (!Number.isInteger(number)) return fallback
  return Math.min(max, Math.max(min, number))
}

function uniqueStrings(values: Array<string | undefined>): string[] {
  return [...new Set(values.filter(isString).map((value) => value.trim()).filter(Boolean))]
}

function uniqueBy<T>(values: T[], key: (value: T) => string): T[] {
  const seen = new Set<string>()
  return values.filter((value) => {
    const id = key(value)
    if (seen.has(id)) return false
    seen.add(id)
    return true
  })
}

function mergeProjects(left: ProjectItem, right: ProjectItem): ProjectItem {
  const relatedPapers = uniqueBy([...(left.relatedPapers ?? []), ...(right.relatedPapers ?? [])], (ref) => ref.url ?? ref.id ?? ref.title)
  const relatedNews = uniqueBy([...(left.relatedNews ?? []), ...(right.relatedNews ?? [])], (ref) => ref.url ?? ref.id ?? ref.title)
  const relatedCommunityTopics = uniqueBy(
    [...(left.relatedCommunityTopics ?? []), ...(right.relatedCommunityTopics ?? [])],
    (ref) => ref.url ?? ref.id ?? ref.title
  )
  const sourceRefs = uniqueBy([...(left.sourceRefs ?? []), ...(right.sourceRefs ?? [])], (ref) => ref.url ?? ref.sourceUrl ?? ref.id ?? "")
  const categories = uniqueBy([...left.categories, ...right.categories], (category) => category)
  const merged: ProjectItem = {
    ...left,
    description: left.description.length >= right.description.length ? left.description : right.description,
    stars: maxDefined(left.stars, right.stars),
    forks: maxDefined(left.forks, right.forks),
    watchers: maxDefined(left.watchers, right.watchers),
    openIssues: maxDefined(left.openIssues, right.openIssues),
    starGrowth24h: maxDefined(left.starGrowth24h, right.starGrowth24h),
    starGrowth7d: maxDefined(left.starGrowth7d, right.starGrowth7d),
    projectMomentum: maxDefined(left.projectMomentum, right.projectMomentum),
    qualityScore: maxDefined(left.qualityScore, right.qualityScore),
    scores: mergeScores(left.scores, right.scores),
    categoryRefs: uniqueBy([...left.categoryRefs, ...right.categoryRefs], (ref) => ref.category),
    categories,
    tags: uniqueStrings([...left.tags, ...right.tags]),
    topics: uniqueStrings([...left.topics, ...right.topics]),
    maturity: left.maturity ?? right.maturity,
    createdAt: earliestDate(left.createdAt, right.createdAt),
    updatedAt: latestDate(left.updatedAt, right.updatedAt),
    pushedAt: latestDate(left.pushedAt, right.pushedAt),
    lastPushedAt: latestDate(left.lastPushedAt, right.lastPushedAt),
    firstSeenAt: earliestDate(left.firstSeenAt, right.firstSeenAt),
    sourceRefs,
    relatedPapers,
    relatedNews,
    relatedCommunityTopics,
    relationCounts: {
      papers: relatedPapers.length,
      news: relatedNews.length,
      community: relatedCommunityTopics.length,
    },
    sources: uniqueBy([...(left.sources ?? []), ...(right.sources ?? [])], (source) => source),
  }
  return stripUndefined(merged)
}

function mergeScores(left: ProjectScores, right: ProjectScores): ProjectScores {
  return stripUndefined({
    trendScore: maxDefined(left.trendScore, right.trendScore),
    starVelocityScore: maxDefined(left.starVelocityScore, right.starVelocityScore),
    freshnessScore: maxDefined(left.freshnessScore, right.freshnessScore),
    activityScore: maxDefined(left.activityScore, right.activityScore),
    adoptionScore: maxDefined(left.adoptionScore, right.adoptionScore),
    evidenceScore: maxDefined(left.evidenceScore, right.evidenceScore),
    qualityScore: maxDefined(left.qualityScore, right.qualityScore),
  })
}

function maxDefined(left: number | undefined, right: number | undefined): number | undefined {
  if (left === undefined) return right
  if (right === undefined) return left
  return Math.max(left, right)
}

function earliestDate(left: string | undefined, right: string | undefined): string | undefined {
  if (!left) return right
  if (!right) return left
  return compareDate(left, right) <= 0 ? left : right
}

function latestDate(left: string | undefined, right: string | undefined): string | undefined {
  if (!left) return right
  if (!right) return left
  return compareDate(left, right) >= 0 ? left : right
}

function stripUndefined<T extends Record<string, unknown>>(value: T): T {
  const cleaned: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(value)) {
    if (item !== undefined) {
      cleaned[key] = item
    }
  }
  return cleaned as T
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
}
