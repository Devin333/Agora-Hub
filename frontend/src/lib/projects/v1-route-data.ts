import { NextResponse } from "next/server"
import { safeApiGet, safeApiPost, type SafeApiResult } from "@/lib/api/server"
import type {
  ProjectItem,
  ProjectListParams,
  ProjectListResult,
  ProjectsApiCase,
  ProjectsApiCaseResult,
  ProjectsApiCollection,
  ProjectsApiCollectionResult,
  ProjectsApiDataState,
  ProjectsApiHomeResult,
  ProjectsApiListResult,
  ProjectsApiMeta,
  ProjectsApiMetric,
  ProjectsApiPageInfo,
  ProjectsApiProject,
  ProjectsApiProjectDetail,
  ProjectsApiSource,
  ProjectsApiTool,
  ProjectsApiToolResult,
  ProjectsApiWatchlistResult,
} from "@/types/projects"

type ApiEnvelope<T> = {
  success: boolean
  data: T | null
  error: { code: string; message: string; details?: unknown } | null
}

export function success<T>(data: T) {
  return NextResponse.json<ApiEnvelope<T>>({ success: true, data, error: null })
}

export function failure(status: number, code: string, message: string, details?: unknown) {
  return NextResponse.json<ApiEnvelope<never>>(
    {
      success: false,
      data: null,
      error: { code, message, details },
    },
    { status }
  )
}

export async function backendGet<T>(path: string): Promise<SafeApiResult<T>> {
  return safeApiGet<T>(path)
}

export async function proxyBackendMutation<T>(path: string, body?: unknown) {
  const result = await safeApiPost<T>(path, body)
  if (result.ok) return success(result.data)
  return failure(
    503,
    "projects_backend_unavailable",
    "Projects mutation endpoints require the Agora Hub API service.",
    result
  )
}

export function backendPath(pathname: string, searchParams?: URLSearchParams) {
  const query = searchParams?.toString()
  return query ? `${pathname}?${query}` : pathname
}

export function projectParams(searchParams: URLSearchParams, defaults: ProjectListParams = {}): ProjectListParams {
  return {
    ...defaults,
    q: searchParams.get("q") ?? defaults.q,
    category: (searchParams.get("category") as ProjectListParams["category"]) ?? defaults.category,
    topic: searchParams.get("topic") ?? defaults.topic,
    sort: (searchParams.get("sort") as ProjectListParams["sort"]) ?? defaults.sort,
    source: (searchParams.get("source") as ProjectListParams["source"]) ?? defaults.source,
    language: (searchParams.get("language") as ProjectListParams["language"]) ?? defaults.language,
    license: (searchParams.get("license") as ProjectListParams["license"]) ?? defaults.license,
    maturity: (searchParams.get("maturity") as ProjectListParams["maturity"]) ?? defaults.maturity,
    period: (searchParams.get("period") as ProjectListParams["period"]) ?? defaults.period,
    page: numberParam(searchParams.get("page")) ?? defaults.page,
    pageSize: numberParam(searchParams.get("page_size")) ?? numberParam(searchParams.get("pageSize")) ?? defaults.pageSize,
    limit: numberParam(searchParams.get("limit")) ?? defaults.limit,
    cursor: searchParams.get("cursor") ?? defaults.cursor,
  }
}

export function buildHomeResult(result: ProjectListResult, limit: number): ProjectsApiHomeResult {
  const all = result.allItems.length ? result.allItems : result.items
  const hot = all
    .slice()
    .sort((left, right) => score(right, "hot") - score(left, "hot"))
    .slice(0, limit)
    .map((project, index) => toApiProject(project, index + 1, "hot"))
  const rising = all
    .slice()
    .sort((left, right) => score(right, "rising") - score(left, "rising"))
    .slice(0, limit)
    .map((project, index) => toApiProject(project, index + 1, "rising"))
  const tools = all.slice(0, limit).map((project, index) => toApiProject(project, index + 1, "hot"))
  const cases = all.flatMap(toDerivedCase).slice(0, limit)

  return {
    hot,
    rising,
    tools,
    cases,
    collections: [],
    watchlist: [],
    recommendations: recommendationCards(result, cases),
    meta: metaFromProjectList(result),
    metrics: metricsFromProjectList(result),
  }
}

export function buildListResult(result: ProjectListResult, scoreType: "hot" | "rising" = "hot"): ProjectsApiListResult {
  return {
    items: result.items.map((project, index) => toApiProject(project, firstRank(result, index), scoreType)),
    page: pageFromProjectList(result),
    meta: metaFromProjectList(result),
    metrics: metricsFromProjectList(result),
  }
}

export function buildToolResult(result: ProjectListResult): ProjectsApiToolResult {
  return {
    tools: result.items.map((project, index) => toApiTool(project, firstRank(result, index))),
    page: pageFromProjectList(result),
    meta: metaFromProjectList(result),
  }
}

export function buildCaseResult(result: ProjectListResult): ProjectsApiCaseResult {
  const cases = (result.allFiltered.length ? result.allFiltered : result.items).flatMap(toDerivedCase)
  const page = pageFromProjectList(result)
  return {
    cases: cases.slice(0, page.page_size),
    page: { ...page, total: cases.length, has_next: cases.length > page.page_size, next_cursor: cases.length > page.page_size ? "2" : null },
    meta: metaFromProjectList(result),
  }
}

export function buildCollectionResult(result: ProjectListResult): ProjectsApiCollectionResult {
  return {
    collections: collectionCards(result),
    meta: metaFromProjectList(result),
  }
}

export function buildWatchlistResult(result: ProjectListResult): ProjectsApiWatchlistResult {
  return {
    items: [],
    meta: {
      ...metaFromProjectList(result),
      notices: [...result.notices, "No local user watchlist state is exposed by the frontend fallback."],
    },
  }
}

export function buildProjectDetail(project: ProjectItem, result: ProjectListResult): ProjectsApiProjectDetail {
  const apiProject = toApiProject(project, undefined, "hot")
  return {
    project: apiProject,
    sources: (project.sourceRefs ?? []).map((source) => ({ ...source })),
    metrics: [apiProject.metric_summary],
    growth: [
      {
        stars_delta_24h: project.starGrowth24h ?? null,
        stars_delta_7d: project.starGrowth7d ?? null,
        project_momentum: project.projectMomentum ?? null,
      },
    ],
    capabilities: capabilityCards(project),
    tool_profile: toApiTool(project).profile,
    cases: toDerivedCase(project),
    collections: [],
    watch_status: null,
    recommended_actions: [
      {
        id: "open_source",
        label: "Review source evidence",
        reason: "Verify public project links and source references before adopting the project.",
      },
    ],
    ranking: {
      hot: { score: apiProject.hot_score ?? null },
      rising: { score: apiProject.rising_score ?? null },
    },
    meta: metaFromProjectList(result),
  }
}

export function findProject(result: ProjectListResult, projectId: string): ProjectItem | undefined {
  const normalized = decodeURIComponent(projectId).toLowerCase()
  return result.allItems.find((project) =>
    [project.id, project.slug, project.name, project.fullName, project.repoUrl]
      .map((value) => value.toLowerCase())
      .includes(normalized)
  )
}

export function findTool(result: ProjectListResult, projectId: string): ProjectsApiTool | undefined {
  const project = findProject(result, projectId)
  return project ? toApiTool(project) : undefined
}

export function findCase(result: ProjectListResult, caseId: string): ProjectsApiCase | undefined {
  const normalized = decodeURIComponent(caseId).toLowerCase()
  return result.allItems.flatMap(toDerivedCase).find((item) => item.id.toLowerCase() === normalized)
}

export function findCollection(result: ProjectListResult, slug: string): ProjectsApiCollection | undefined {
  const normalized = decodeURIComponent(slug).toLowerCase()
  return collectionCards(result).find((item) => item.slug.toLowerCase() === normalized || item.id.toLowerCase() === normalized)
}

function toApiProject(project: ProjectItem, rank?: number, scoreType: "hot" | "rising" = "hot"): ProjectsApiProject {
  const hotScore = score(project, "hot")
  const risingScore = score(project, "rising")
  return {
    id: project.id,
    slug: project.slug,
    name: project.name,
    tagline: project.problemSolved ?? project.whyItMatters ?? null,
    description: project.description,
    canonical_url: project.repoUrl,
    website_url: project.homepageUrl ?? null,
    github_url: project.repoUrl,
    docs_url: null,
    demo_url: null,
    project_type: "project",
    category: project.categoryRefs[0]?.label ?? project.categories[0] ?? null,
    tags: [...new Set([...project.tags, ...project.topics])],
    source_confidence: project.sourceRefs?.length ? 1 : 0.7,
    hot_score: scoreType === "hot" ? hotScore : null,
    rising_score: scoreType === "rising" ? risingScore : null,
    rank: rank ?? null,
    rank_reason: rankReason(project),
    metric_summary: metricSummary(project),
    capability_count: project.categoryRefs.length + project.tags.length,
    case_count: toDerivedCase(project).length,
    source_count: project.sourceRefs?.length ?? project.relationCounts.news + project.relationCounts.community + project.relationCounts.papers,
    updated_at: project.updatedAt ?? project.pushedAt ?? project.lastPushedAt ?? null,
  }
}

function toApiTool(project: ProjectItem, rank?: number): ProjectsApiTool {
  const apiProject = toApiProject(project, rank, "hot")
  const category = project.categoryRefs[0]?.label ?? project.categories[0] ?? "project"
  return {
    project: apiProject,
    profile: {
      project_id: project.id,
      tool_type: category,
      input_types: project.topics.slice(0, 4),
      output_types: project.tags.slice(0, 4),
      is_open_source: Boolean(project.repoUrl),
      license: project.license ?? null,
      local_deployable: Boolean(project.repoUrl),
      has_api: null,
      has_cli: null,
      has_python_sdk: (project.language ?? "").toLowerCase().includes("python") || null,
      has_docker: null,
      integration_difficulty: integrationDifficulty(project),
      recommended_integration: "reference_only",
      target_modules: [...new Set([...project.categories, ...project.topics])].slice(0, 6),
      setup_commands: [],
      usage_example: null,
      known_limits: [],
      experiment_status: "untested",
    },
    capabilities: capabilityCards(project),
    fit_reason: project.whyItMatters ?? project.problemSolved ?? "Derived from real Project Radar project metadata.",
  }
}

function toDerivedCase(project: ProjectItem): ProjectsApiCase[] {
  const summary = project.problemSolved ?? project.whyItMatters
  if (!summary) return []
  const category = project.categoryRefs[0]?.label ?? project.categories[0] ?? "project"
  return [
    {
      id: `case-${project.slug}`,
      project_id: project.id,
      title: `${project.name} adoption case`,
      business_domain: category,
      module_type: category,
      problem: project.problemSolved ?? project.description,
      design_summary: summary,
      source_refs: project.sourceRefs ?? [],
      project_slug: project.slug,
    },
  ]
}

function collectionCards(result: ProjectListResult): ProjectsApiCollection[] {
  const options = result.options.categories.slice(0, 8)
  return options.map((option) => ({
    id: `collection-${option.value}`,
    slug: String(option.value),
    title: `${option.label} Projects`,
    description: `Real Project Radar projects grouped under ${option.label}.`,
    item_count: option.count,
    sections: [
      {
        title: option.label,
        items: result.allItems
          .filter((project) => project.categories.some((category) => category === option.value))
          .slice(0, 12)
          .map((project) => ({ item_type: "project", item_id: project.id, title: project.name })),
      },
    ],
  }))
}

function capabilityCards(project: ProjectItem): Array<Record<string, unknown>> {
  return [...project.categoryRefs.map((ref) => ref.label), ...project.tags, ...project.topics]
    .filter(Boolean)
    .slice(0, 12)
    .map((label) => ({ label, source: "project_radar_artifact" }))
}

function metaFromProjectList(result: ProjectListResult): ProjectsApiMeta {
  return {
    source: result.source as ProjectsApiSource,
    source_run_id: result.sourceRunId ?? null,
    generated_at: result.generatedAt ?? null,
    data_state: result.dataState as ProjectsApiDataState,
    notices: result.notices,
  }
}

function metricsFromProjectList(result: ProjectListResult): ProjectsApiMetric[] {
  return result.metrics.map((metric) => ({ label: metric.label, value: metric.value, hint: metric.hint ?? null }))
}

function pageFromProjectList(result: ProjectListResult): ProjectsApiPageInfo {
  return {
    page: result.page.page,
    page_size: result.page.pageSize,
    total: result.page.total,
    has_next: result.page.hasNext,
    next_cursor: result.page.nextCursor ?? null,
  }
}

function metricSummary(project: ProjectItem): Record<string, unknown> {
  return {
    github_stars: project.stars ?? null,
    github_forks: project.forks ?? null,
    open_issues: project.openIssues ?? null,
    quality_score: project.qualityScore ?? project.scores.qualityScore ?? null,
    activity_score: project.scores.activityScore ?? null,
    evidence_score: project.scores.evidenceScore ?? null,
    stars_delta_7d: project.starGrowth7d ?? null,
    stars_delta_24h: project.starGrowth24h ?? null,
  }
}

function recommendationCards(result: ProjectListResult, cases: ProjectsApiCase[]): Array<Record<string, unknown>> {
  const recommendations: Array<Record<string, unknown>> = []
  if (result.allItems.length) {
    recommendations.push({
      type: "projects",
      title: "Review top Project Radar candidates",
      reason: `${result.allItems.length} real project records are available from the current source.`,
    })
  }
  if (cases.length) {
    recommendations.push({
      type: "cases",
      title: "Open derived module cases",
      reason: `${cases.length} real-derived cases include adoption context.`,
    })
  }
  return recommendations
}

function firstRank(result: ProjectListResult, index: number): number {
  return (result.page.page - 1) * result.page.pageSize + index + 1
}

function score(project: ProjectItem, scoreType: "hot" | "rising"): number {
  if (scoreType === "rising") {
    return project.scores.starVelocityScore ?? project.starGrowth7d ?? project.starGrowth24h ?? project.scores.freshnessScore ?? 0
  }
  return project.scores.trendScore ?? project.projectMomentum ?? project.qualityScore ?? project.stars ?? 0
}

function rankReason(project: ProjectItem): string {
  const signals = [
    project.starGrowth7d ? `${project.starGrowth7d} stars gained recently` : undefined,
    project.relationCounts.news ? `${project.relationCounts.news} news references` : undefined,
    project.relationCounts.community ? `${project.relationCounts.community} community references` : undefined,
  ].filter(Boolean)
  return signals.length ? signals.join("; ") : "Ranked from real Project Radar artifact fields."
}

function integrationDifficulty(project: ProjectItem): "low" | "medium" | "high" {
  const text = `${project.description} ${project.tags.join(" ")} ${project.topics.join(" ")}`.toLowerCase()
  if (text.includes("framework") || text.includes("platform") || text.includes("infra")) return "high"
  if (text.includes("library") || text.includes("sdk") || text.includes("tool")) return "medium"
  return "medium"
}

function numberParam(value: string | null): number | undefined {
  if (!value) return undefined
  const number = Number(value)
  return Number.isFinite(number) ? number : undefined
}
