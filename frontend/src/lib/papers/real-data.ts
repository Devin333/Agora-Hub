import fs from "node:fs"
import path from "node:path"
import { safeApiGet } from "@/lib/api/server"
import { normalizePdfUrl, paperPdfUrlFromSource, sortPapers } from "@/lib/papers/format"
import { queryPapers, type PaperQuery } from "@/lib/papers/query"
import { paperMethods, paperTasks } from "@/lib/papers/catalog"
import { arxivIdFromUrl, enrichPapersForPublicStream, githubRepoSlug, normalizeDoi, normalizeGithubRepoUrl } from "@/lib/papers/enrichment"
import type {
  BenchmarkRef,
  MethodRef,
  Paper,
  PaperBenchmarkResult,
  PaperDataState,
  PaperImplementation,
  PaperListResult,
  PaperMethod,
  PaperTask,
  TaskRef
} from "@/lib/papers/types"

type SourceSignal = {
  authors?: unknown
  collected_at?: unknown
  confidence?: unknown
  content?: unknown
  evidence_type?: unknown
  lineage?: Record<string, unknown>
  metadata?: Record<string, unknown>
  metrics?: Record<string, unknown>
  published_at?: unknown
  raw_content?: unknown
  raw_payload?: SourceSignal
  signal_id?: unknown
  signal_type?: unknown
  source?: {
    source_name?: unknown
    source_type?: unknown
    source_url?: unknown
    url?: unknown
  }
  source_id?: unknown
  source_item_id?: unknown
  source_name?: unknown
  source_reliability?: unknown
  source_url?: unknown
  source_urls?: unknown
  source_type?: unknown
  summary?: unknown
  tags?: unknown
  title?: unknown
  url?: unknown
}

type PaperRadarArtifact = {
  board_signals?: SourceSignal[]
  evidence_bundle?: { items?: SourceSignal[] }
  items?: SourceSignal[]
  normalized_items?: SourceSignal[]
  ranked_items?: Array<{ item?: SourceSignal; final_score?: unknown }>
  raw_signals?: SourceSignal[]
  raw_items?: SourceSignal[]
  ranked_signals?: Array<{ item?: SourceSignal; final_score?: unknown }>
}

type PaperCollectionCache = {
  source?: unknown
  query?: unknown
  collectedAt?: unknown
  count?: unknown
  papers?: unknown
}

type PapersApiResponse = {
  papers?: unknown
}

type PaperTasksApiResponse = {
  tasks?: unknown
}

type PaperMethodsApiResponse = {
  methods?: unknown
}

const ARTIFACT_FILE_NAMES = [
  "data_buffer.final.json",
  "output.json",
  "raw_items.json",
  "normalized_items.json",
  "ranked_items.json"
]
const MAX_ARTIFACT_BYTES = 8_000_000
const API_PAPER_LOAD_LIMIT = 5000
const PAPER_SOURCE_TYPES = new Set(["arxiv", "paper_index"])
const BLOCKED_SOURCE_TYPES = new Set(["official_blog", "ai_news", "rss", "blog", "press_release"])
const PAPERS_DATA_PATH_ENV = "NEWSROOM_PAPERS_DATA_PATH"
const SHARED_PAPER_CACHE_PATH = path.resolve(projectRoot(), ".newsroom", "papers", "arxiv-papers.json")
const LEGACY_FRONTEND_PAPER_CACHE_PATH = path.resolve(projectRoot(), "frontend", "data", "papers", "arxiv-papers.json")
const TASK_SLUG_ALIASES: Record<string, string> = {
  "task-agent-task-completion": "agent-task-completion",
  "task-language-models": "language-models"
}
const METHOD_SLUG_ALIASES: Record<string, string> = {
  "method-language-models": "language-models",
  "method-large-language-models": "large-language-model"
}
const PAPER_CACHE_NOTICE = "Using verified cached paper data while the live paper index refreshes."
const PAPER_ARTIFACT_NOTICE = "Using the latest verified Paper Radar artifacts while cached paper data refreshes."
const PAPER_EMPTY_NOTICE = "No verified public paper data is available yet."
const TASK_TAXONOMY_NOTICE = "Task taxonomy is built from verified paper references while the curated directory refreshes."
const METHOD_TAXONOMY_NOTICE = "Method taxonomy is built from verified paper references while the curated directory refreshes."

export type PaperRuntimeData = {
  papers: Paper[]
  source: "backend" | "cache" | "artifact" | "empty"
  dataState: PaperDataState
  notices: string[]
  collectedAt?: string
}

export type PaperResearchDataset = PaperRuntimeData & {
  tasks: PaperTask[]
  methods: PaperMethod[]
  taskTaxonomySource: "backend" | "taxonomy"
  methodTaxonomySource: "backend" | "taxonomy"
}

export type PaperListQuery = PaperQuery & {
  limit?: number
  offset?: number
}

export type PaperTaxonomyResult<T> = {
  items: T[]
  source: "backend" | "taxonomy"
  dataState: PaperDataState
  notices: string[]
}

export async function getPublishedPapers() {
  return (await getPublishedPaperData()).papers
}

export async function getPublishedPaperData(): Promise<PaperRuntimeData> {
  const apiPapers = await loadApiPapers()
  const publicApiPapers = normalizeRuntimePapers(apiPapers)
  if (publicApiPapers.length) {
    return {
      papers: publicApiPapers,
      source: "backend",
      dataState: "ready",
      notices: []
    }
  }

  const cachedPapers = loadCachedPapers()
  const publicCachedPapers = normalizeRuntimePapers(cachedPapers)
  if (publicCachedPapers.length) {
    return {
      papers: publicCachedPapers,
      source: "cache",
      dataState: "degraded",
      notices: [PAPER_CACHE_NOTICE],
      collectedAt: latestPaperTimestamp(publicCachedPapers)
    }
  }

  const extractedPapers = loadLatestExtractedPapers()
  const publicExtractedPapers = normalizeRuntimePapers(await enrichPapersForPublicStream(extractedPapers))
  if (publicExtractedPapers.length) {
    return {
      papers: publicExtractedPapers,
      source: "artifact",
      dataState: "degraded",
      notices: [PAPER_ARTIFACT_NOTICE],
      collectedAt: latestPaperTimestamp(publicExtractedPapers)
    }
  }

  return {
    papers: [],
    source: "empty",
    dataState: "empty",
    notices: [PAPER_EMPTY_NOTICE]
  }
}

export async function getPaperListResult(query: PaperListQuery = {}): Promise<PaperListResult> {
  const data = await getPublishedPaperData()
  const limit = positiveInteger(query.limit, 1000)
  const offset = Math.max(0, positiveInteger(query.offset, 0))
  const queried = queryPapers(data.papers, query)
  const sorted = queried.papers
  const papers = sorted.slice(offset, offset + limit)

  return {
    source: data.source,
    query: queried.query.q,
    period: queried.query.period,
    sort: queried.query.sort,
    task: queried.query.task,
    method: queried.query.method,
    topic: queried.query.topic,
    from: queried.query.from,
    to: queried.query.to,
    collectedAt: data.collectedAt ?? new Date().toISOString(),
    earliestPublishedAt: queried.earliestPublishedAt,
    latestPublishedAt: queried.latestPublishedAt,
    paper_count: papers.length,
    total_count: sorted.length,
    source_count: uniqueStrings(data.papers.map((paper) => paper.venue ?? paper.sourceRefs?.[0]?.sourceName ?? "papers")).length,
    limit,
    offset,
    has_next: offset + papers.length < sorted.length,
    dataState: data.dataState,
    notices: data.notices,
    papers
  }
}

export async function getPaperById(paperId: string): Promise<Paper | null> {
  const result = await safeApiGet<{ paper?: unknown }>(`/api/v1/papers/${encodeURIComponent(paperId)}`)
  if (result.ok && isPaper(result.data?.paper)) {
    return normalizePublicPaper(result.data.paper)
  }
  if (result.ok && isPaper(result.data)) {
    return normalizePublicPaper(result.data)
  }

  const data = await getPublishedPaperData()
  return data.papers.find((paper) => paper.id === paperId || paper.slug === paperId) ?? null
}

export async function getPaperTasksResult(): Promise<PaperTaxonomyResult<PaperTask>> {
  const [apiTasks, paperData] = await Promise.all([loadApiPaperTasks(), getPublishedPaperData()])
  const source = apiTasks.length ? "backend" : "taxonomy"
  const tasks = deriveTasks(apiTasks, paperData.papers)
  return {
    items: tasks,
    source,
    dataState: taxonomyDataState(apiTasks.length > 0, paperData.dataState),
    notices: apiTasks.length
      ? paperData.notices
      : [TASK_TAXONOMY_NOTICE, ...paperData.notices]
  }
}

export async function getPaperMethodsResult(): Promise<PaperTaxonomyResult<PaperMethod>> {
  const [apiMethods, apiTasks, paperData] = await Promise.all([loadApiPaperMethods(), loadApiPaperTasks(), getPublishedPaperData()])
  const source = apiMethods.length ? "backend" : "taxonomy"
  const tasks = deriveTasks(apiTasks, paperData.papers)
  const methods = deriveMethods(apiMethods, tasks, paperData.papers)
  return {
    items: methods,
    source,
    dataState: taxonomyDataState(apiMethods.length > 0, paperData.dataState),
    notices: apiMethods.length
      ? paperData.notices
      : [METHOD_TAXONOMY_NOTICE, ...paperData.notices]
  }
}

export async function getPaperResearchDataset(): Promise<PaperResearchDataset> {
  const [apiTasks, apiMethods, paperData] = await Promise.all([
    loadApiPaperTasks(),
    loadApiPaperMethods(),
    getPublishedPaperData()
  ])
  const tasks = deriveTasks(apiTasks, paperData.papers)
  return {
    ...paperData,
    tasks,
    methods: deriveMethods(apiMethods, tasks, paperData.papers),
    taskTaxonomySource: apiTasks.length ? "backend" : "taxonomy",
    methodTaxonomySource: apiMethods.length ? "backend" : "taxonomy"
  }
}

export async function loadApiPapers(): Promise<Paper[]> {
  const result = await safeApiGet<PapersApiResponse>(`/api/v1/papers?limit=${API_PAPER_LOAD_LIMIT}`)
  if (!result.ok) {
    return []
  }

  const rawPapers = Array.isArray(result.data?.papers) ? result.data.papers : []
  return rawPapers.filter(isAuthoritativePaper)
}

export async function loadApiPaperTasks(): Promise<PaperTask[]> {
  const result = await safeApiGet<PaperTasksApiResponse>("/api/v1/papers/tasks")
  if (!result.ok || !Array.isArray(result.data?.tasks)) {
    return []
  }
  return result.data.tasks.filter(isPaperTask)
}

export async function loadApiPaperMethods(): Promise<PaperMethod[]> {
  const result = await safeApiGet<PaperMethodsApiResponse>("/api/v1/papers/methods")
  if (!result.ok || !Array.isArray(result.data?.methods)) {
    return []
  }
  return result.data.methods.filter(isPaperMethod)
}

function normalizeRuntimePapers(papers: Paper[]) {
  return papers.filter((paper) => paper.isPublished !== false).map(normalizeRuntimePaper)
}

function normalizePublicPaper(paper: Paper) {
  return paper.isPublished === false ? null : normalizeRuntimePaper(paper)
}

function normalizeRuntimePaper(paper: Paper): Paper {
  const repoUrl = normalizeGithubRepoUrl(paper.repoUrl)
  const taskRefs = taskRefsFromRecord(paper as unknown as Record<string, unknown>)
  const methodRefs = methodRefsFromRecord(paper as unknown as Record<string, unknown>)
  const implementations = paper.implementations?.length
    ? paper.implementations
    : repoUrl
      ? [
          {
            id: `${paper.id}-repo`,
            name: githubRepoSlug(repoUrl) ?? "GitHub repository",
            repoUrl,
            provider: "GitHub",
            githubStars: paper.githubStars
          }
        ]
      : []

  return {
    ...paper,
    tags: Array.isArray(paper.tags) ? paper.tags : [],
    taskRefs,
    methodRefs,
    repoUrl,
    implementations
  }
}

function taxonomyDataState(hasApiTaxonomy: boolean, paperDataState: PaperDataState): PaperDataState {
  if (!hasApiTaxonomy) {
    return paperDataState === "empty" ? "empty" : "degraded"
  }
  return paperDataState
}

function deriveTasks(tasks: PaperTask[], papers: Paper[]): PaperTask[] {
  const metadataBySlug = taskMetadataBySlug(tasks)
  const records = new Map<string, {
    ref: TaskRef
    papers: Paper[]
    methods: Map<string, MethodRef>
    sisterTasks: Map<string, TaskRef>
    benchmarks: Map<string, PaperBenchmarkResult>
  }>()

  for (const paper of papers.filter((item) => item.isPublished !== false)) {
    for (const task of uniqueRefs((paper.taskRefs ?? []).map(normalizeTaskRef))) {
      if (!task.slug) {
        continue
      }
      const record = records.get(task.slug) ?? {
        ref: task,
        papers: [],
        methods: new Map<string, MethodRef>(),
        sisterTasks: new Map<string, TaskRef>(),
        benchmarks: new Map<string, PaperBenchmarkResult>()
      }
      record.papers.push(paper)
      for (const method of uniqueRefs((paper.methodRefs ?? []).map(normalizeMethodRef))) {
        if (method.slug) {
          record.methods.set(method.slug, method)
        }
      }
      for (const sibling of uniqueRefs((paper.taskRefs ?? []).map(normalizeTaskRef))) {
        if (sibling.slug && sibling.slug !== task.slug) {
          record.sisterTasks.set(sibling.slug, sibling)
        }
      }
      for (const benchmark of paper.benchmarks ?? []) {
        if (!benchmark.taskSlug || canonicalTaskSlug(benchmark.taskSlug) === task.slug) {
          record.benchmarks.set(benchmark.id || benchmark.name, benchmark)
        }
      }
      records.set(task.slug, record)
    }
  }

  return Array.from(records.values())
    .map((record) => {
      const metadata = metadataBySlug.get(record.ref.slug)
      const relatedPapers = record.papers
      return {
        id: record.ref.id || metadata?.id || `task-${record.ref.slug}`,
        slug: record.ref.slug,
        name: record.ref.name || metadata?.name || titleizeSlug(record.ref.slug),
        nameZh: cleanLocalizedText(record.ref.nameZh) ?? cleanLocalizedText(metadata?.nameZh),
        group: record.ref.group || metadata?.group || "general",
        description: metadata?.description || `Aggregated from ${relatedPapers.length} public papers tagged with ${record.ref.name || titleizeSlug(record.ref.slug)}.`,
        descriptionZh: cleanLocalizedText(metadata?.descriptionZh) ?? `由 ${relatedPapers.length} 篇真实公开论文引用聚合。`,
        paperCount: relatedPapers.length,
        benchmarkCount: record.benchmarks.size,
        methodCount: record.methods.size,
        sisterTasks: topRefs(record.sisterTasks.values()),
        commonMethods: topRefs(record.methods.values()),
        latestPaperIds: sortPapers(relatedPapers, "newest").slice(0, 5).map((paper) => paper.id),
        implementationCount: countImplementationSignals(relatedPapers)
      }
    })
    .sort((left, right) => right.paperCount - left.paperCount || left.name.localeCompare(right.name))
}

function deriveMethods(methods: PaperMethod[], tasks: PaperTask[], papers: Paper[]): PaperMethod[] {
  const metadataBySlug = methodMetadataBySlug(methods)
  const taskBySlug = new Map(tasks.map((task) => [task.slug, task]))
  const records = new Map<string, {
    ref: MethodRef
    papers: Paper[]
    tasks: Map<string, TaskRef>
    relatedMethods: Map<string, MethodRef>
    benchmarks: Map<string, BenchmarkRef>
  }>()

  for (const paper of papers.filter((item) => item.isPublished !== false)) {
    for (const method of uniqueRefs((paper.methodRefs ?? []).map(normalizeMethodRef))) {
      if (!method.slug) {
        continue
      }
      const record = records.get(method.slug) ?? {
        ref: method,
        papers: [],
        tasks: new Map<string, TaskRef>(),
        relatedMethods: new Map<string, MethodRef>(),
        benchmarks: new Map<string, BenchmarkRef>()
      }
      record.papers.push(paper)
      for (const task of uniqueRefs((paper.taskRefs ?? []).map(normalizeTaskRef))) {
        if (task.slug) {
          record.tasks.set(task.slug, task)
        }
      }
      for (const sibling of uniqueRefs((paper.methodRefs ?? []).map(normalizeMethodRef))) {
        if (sibling.slug && sibling.slug !== method.slug) {
          record.relatedMethods.set(sibling.slug, sibling)
        }
      }
      for (const benchmark of paper.benchmarks ?? []) {
        const ref = benchmarkRef(benchmark)
        record.benchmarks.set(ref.id, ref)
      }
      records.set(method.slug, record)
    }
  }

  return Array.from(records.values())
    .map((record) => {
      const metadata = metadataBySlug.get(record.ref.slug)
      const relatedPapers = record.papers
      const relatedTasks = Array.from(record.tasks.values())
        .map((task) => taskBySlug.get(task.slug) ?? task)
        .filter(isTaskRef)
      return {
        id: record.ref.id || metadata?.id || `method-${record.ref.slug}`,
        slug: record.ref.slug,
        name: record.ref.name || metadata?.name || titleizeSlug(record.ref.slug),
        nameZh: cleanLocalizedText(record.ref.nameZh) ?? cleanLocalizedText(metadata?.nameZh),
        description: metadata?.description || `Aggregated from ${relatedPapers.length} public papers using ${record.ref.name || titleizeSlug(record.ref.slug)}.`,
        descriptionZh: cleanLocalizedText(metadata?.descriptionZh) ?? `由 ${relatedPapers.length} 篇真实公开论文引用聚合。`,
        paperCount: relatedPapers.length,
        taskCount: record.tasks.size,
        implementationCount: countImplementationSignals(relatedPapers),
        area: record.ref.area || metadata?.area || methodArea(record.ref.name || metadata?.name || record.ref.slug),
        relatedTasks: topRefs(relatedTasks),
        relatedMethods: topRefs(record.relatedMethods.values()),
        commonBenchmarks: Array.from(record.benchmarks.values()).slice(0, 8),
        representativePaperIds: sortPapers(relatedPapers, "trending").slice(0, 5).map((paper) => paper.id),
        relatedProjectIds: relatedProjectIds(relatedPapers)
      }
    })
    .sort((left, right) => right.paperCount - left.paperCount || left.name.localeCompare(right.name))
}

function taskMetadataBySlug(apiTasks: PaperTask[]) {
  const records = new Map<string, PaperTask>()
  for (const task of paperTasks) {
    records.set(canonicalTaskSlug(task.slug), { ...task, slug: canonicalTaskSlug(task.slug) })
  }
  for (const task of apiTasks) {
    const slug = canonicalTaskSlug(task.slug)
    records.set(slug, { ...task, slug })
  }
  return records
}

function methodMetadataBySlug(apiMethods: PaperMethod[]) {
  const records = new Map<string, PaperMethod>()
  for (const method of paperMethods) {
    records.set(canonicalMethodSlug(method.slug), { ...method, slug: canonicalMethodSlug(method.slug) })
  }
  for (const method of apiMethods) {
    const slug = canonicalMethodSlug(method.slug)
    records.set(slug, { ...method, slug })
  }
  return records
}

function topRefs<T extends { name: string; slug: string }>(refs: Iterable<T>): T[] {
  return Array.from(refs)
    .filter((ref) => ref.slug && ref.name)
    .sort((left, right) => left.name.localeCompare(right.name))
    .slice(0, 8)
}

function benchmarkRef(benchmark: PaperBenchmarkResult): BenchmarkRef {
  const slug = slugify(benchmark.name) || benchmark.id
  return {
    id: benchmark.id || slug,
    slug,
    name: benchmark.name,
    category: benchmark.category
  }
}

function countImplementationSignals(papers: Paper[]) {
  return implementationKeys(papers).size
}

function relatedProjectIds(papers: Paper[]) {
  return Array.from(implementationKeys(papers))
}

function implementationKeys(papers: Paper[]) {
  const keys = new Set<string>()
  for (const paper of papers) {
    addImplementationKey(keys, paper.repoUrl)
    for (const implementation of paper.implementations ?? []) {
      addImplementationKey(keys, implementation.repoUrl || implementation.id)
    }
  }
  return keys
}

function addImplementationKey(keys: Set<string>, value?: string) {
  const normalized = normalizeGithubRepoUrl(value) ?? text(value).toLowerCase().replace(/\.git$/i, "")
  if (normalized) {
    keys.add(normalized)
  }
}

function methodArea(value: string) {
  const normalized = lower(value)
  if (normalized.includes("agent") || normalized.includes("tool") || normalized.includes("planning")) {
    return "Agents"
  }
  if (normalized.includes("retrieval") || normalized.includes("rag") || normalized.includes("knowledge")) {
    return "Retrieval"
  }
  if (normalized.includes("vision") || normalized.includes("multimodal")) {
    return "Multimodal"
  }
  if (normalized.includes("language") || normalized.includes("llm") || normalized.includes("model")) {
    return "Language Models"
  }
  return "Unclassified"
}

function normalizeTaskRef(ref: TaskRef): TaskRef {
  const slug = canonicalTaskSlug(ref.slug || ref.name)
  const metadata = taskMetadata(slug)
  return {
    ...ref,
    id: metadata?.id || ref.id || `task-${slug}`,
    slug,
    name: ref.name || metadata?.name || titleizeSlug(slug),
    nameZh: cleanLocalizedText(ref.nameZh) ?? cleanLocalizedText(metadata?.nameZh),
    group: ref.group || metadata?.group
  }
}

function normalizeMethodRef(ref: MethodRef): MethodRef {
  const slug = canonicalMethodSlug(ref.slug || ref.name)
  const metadata = methodMetadata(slug)
  return {
    ...ref,
    id: metadata?.id || ref.id || `method-${slug}`,
    slug,
    name: ref.name || metadata?.name || titleizeSlug(slug),
    nameZh: cleanLocalizedText(ref.nameZh) ?? cleanLocalizedText(metadata?.nameZh),
    area: ref.area || metadata?.area
  }
}

function canonicalTaskSlug(value: string) {
  return canonicalTaxonomySlug(value, "task", TASK_SLUG_ALIASES)
}

function canonicalMethodSlug(value: string) {
  return canonicalTaxonomySlug(value, "method", METHOD_SLUG_ALIASES)
}

function canonicalTaxonomySlug(value: string, kind: "task" | "method", aliases: Record<string, string>) {
  const normalized = slugify(value) || lower(value).trim()
  if (!normalized) {
    return ""
  }
  const aliased = aliases[normalized]
  if (aliased) {
    return aliased
  }
  const prefix = `${kind}-`
  if (normalized.startsWith(prefix) && normalized.length > prefix.length) {
    return normalized.slice(prefix.length)
  }
  return normalized
}

function titleizeSlug(value: string) {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ")
}

function cleanLocalizedText(value: string | undefined) {
  const cleaned = text(value)
  if (!cleaned || /[�€�]|銆|鏅|璁|鐨|浠|绾|鍏|涓|鎺|妯|涔|鍥|闈|瑙|噯|瀹|鐮|蹇|淇|杈|紝/.test(cleaned)) {
    return undefined
  }
  return cleaned
}

function isTaskRef(value: unknown): value is TaskRef {
  return isRecord(value) && Boolean(text(value.slug) && text(value.name))
}

function positiveInteger(value: number | undefined, fallback: number) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback
}

function isAuthoritativePaper(value: unknown): value is Paper {
  if (!isRecord(value)) {
    return false
  }
  return Boolean(
    text(value.id) &&
      text(value.slug) &&
      text(value.title) &&
      text(value.abstractSnippet) &&
      Array.isArray(value.authors) &&
      text(value.publishedAt) &&
      value.isPublished !== undefined
  )
}

function isPaperTask(value: unknown): value is PaperTask {
  return (
    isRecord(value) &&
    text(value.id) !== "" &&
    text(value.slug) !== "" &&
    text(value.name) !== "" &&
    text(value.description) !== "" &&
    typeof value.paperCount === "number" &&
    Array.isArray(value.sisterTasks) &&
    Array.isArray(value.commonMethods)
  )
}

function isPaperMethod(value: unknown): value is PaperMethod {
  return (
    isRecord(value) &&
    text(value.id) !== "" &&
    text(value.slug) !== "" &&
    text(value.name) !== "" &&
    text(value.description) !== "" &&
    typeof value.paperCount === "number" &&
    typeof value.taskCount === "number" &&
    Array.isArray(value.relatedTasks) &&
    Array.isArray(value.relatedMethods)
  )
}

export function loadCachedPapers(): Paper[] {
  for (const cachePath of paperCachePaths()) {
    const cache = readPaperCollectionCache(cachePath)
    if (cache) {
      const rawPapers = Array.isArray(cache.papers) ? cache.papers : []
      return rawPapers.map(cachedPaperToPaper).filter(isPaper)
    }
  }
  return []
}

function paperCachePaths() {
  const configuredPath = process.env[PAPERS_DATA_PATH_ENV]?.trim()
  if (configuredPath) {
    return [path.resolve(projectRoot(), configuredPath)]
  }
  return uniqueStrings([SHARED_PAPER_CACHE_PATH, LEGACY_FRONTEND_PAPER_CACHE_PATH])
}

function projectRoot() {
  const cwd = process.cwd()
  return path.basename(cwd) === "frontend" ? path.resolve(cwd, "..") : cwd
}

function readPaperCollectionCache(filePath: string): PaperCollectionCache | null {
  if (!fs.existsSync(filePath)) {
    return null
  }

  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8")) as PaperCollectionCache
  } catch {
    return null
  }
}

function cachedPaperToPaper(value: unknown): Paper | null {
  if (!isRecord(value)) {
    return null
  }

  const title = text(value.title)
  const abstractSnippet = stripHtml(text(value.abstractSnippet) || text(value.summary))
  const id = text(value.id)
  const paperUrl = text(value.paperUrl)
  const arxivUrl = text(value.arxivUrl) || paperUrl
  const pdfUrl = normalizePdfUrl(text(value.pdfUrl)) ?? paperPdfUrlFromSource(arxivUrl)

  if (!id || !title || !abstractSnippet || !paperUrl) {
    return null
  }

  const tags = cachedTags(value.tags)
  const taskRefs = taskRefsFromRecord(value)
  const methodRefs = methodRefsFromRecord(value)
  const repoUrl = normalizeGithubRepoUrl(text(value.repoUrl))
  const implementations = implementationsFromRecord(value)
  const benchmarks = benchmarksFromRecord(value)

  return {
    id,
    slug: text(value.slug) || slugify(title) || id,
    title,
    abstractSnippet,
    authors: cachedAuthors(value.authors),
    publishedAt: text(value.publishedAt) || new Date().toISOString(),
    venue: text(value.venue) || "arXiv",
    citationDoi: normalizeDoi(text(value.citationDoi)),
    tags,
    taskRefs,
    methodRefs,
    paperUrl,
    arxivUrl,
    pdfUrl,
    repoUrl,
    implementations,
    benchmarks,
    sourceRefs: [
      {
        sourceId: text(value.sourceId) || id,
        sourceName: text(value.sourceName) || text(value.venue) || "arXiv",
        sourceType: text(value.sourceType) || "arxiv",
        url: paperUrl,
        title
      }
    ],
    githubStars: numberValue(value.githubStars),
    citationCount: numberValue(value.citationCount),
    isPublished: value.isPublished !== false
  }
}

function loadLatestExtractedPapers(): Paper[] {
  const artifacts = candidateArtifacts()
  for (const artifactPath of artifacts) {
    const artifact = readArtifact(artifactPath)
    if (!artifact) {
      continue
    }

    const signals = dedupeSignals(extractSignals(artifact).filter(isPaperSignal))
    const papers = signals.map((signal, index) => signalToPaper(signal, index)).filter(isPaper)
    if (papers.length) {
      return papers
    }
  }

  return []
}

function candidateArtifacts() {
  const runsDir = path.resolve(process.cwd(), "..", ".newsroom", "runs")
  if (!fs.existsSync(runsDir)) {
    return []
  }

  return fs.readdirSync(runsDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .flatMap((entry) => {
      const runDir = path.join(runsDir, entry.name)
      return ARTIFACT_FILE_NAMES.map((fileName) => path.join(runDir, fileName))
    })
    .filter((filePath) => {
      if (!fs.existsSync(filePath)) {
        return false
      }
      const stat = fs.statSync(filePath)
      return stat.isFile() && stat.size > 2 && stat.size <= MAX_ARTIFACT_BYTES
    })
    .sort((left, right) => fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs)
}

function readArtifact(filePath: string): PaperRadarArtifact | SourceSignal[] | null {
  if (!fs.existsSync(filePath)) {
    return null
  }

  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8")) as PaperRadarArtifact | SourceSignal[]
  } catch {
    return null
  }
}

function extractSignals(artifact: PaperRadarArtifact | SourceSignal[]): SourceSignal[] {
  if (Array.isArray(artifact)) {
    return arraySignals(artifact)
  }

  return [
    ...arraySignals(artifact.board_signals),
    ...arraySignals(artifact.raw_signals),
    ...arraySignals(artifact.raw_items),
    ...arraySignals(artifact.normalized_items),
    ...arraySignals(artifact.items),
    ...arraySignals(artifact.evidence_bundle?.items),
    ...rankedSignals(artifact.ranked_signals),
    ...rankedSignals(artifact.ranked_items)
  ]
}

function isPaperSignal(signal: SourceSignal) {
  const raw = isObject(signal.raw_payload) ? signal.raw_payload : signal
  const sourceType = lower(text(signal.source_type) || text(raw.source_type) || text(signal.source?.source_type))
  const sourceName = lower(text(signal.source_name) || text(raw.source_name) || text(signal.source?.source_name))
  const signalType = lower(text(signal.signal_type) || text(raw.signal_type))
  const sourceId = lower(text(signal.source_id) || text(raw.source_id))
  const evidenceType = lower(text(signal.evidence_type) || text(raw.evidence_type))
  const metadataSignalKind = lower(text(signal.metadata?.signal_kind) || text(raw.metadata?.signal_kind))
  const metadataSourceKind = lower(text(signal.metadata?.source_kind) || text(raw.metadata?.source_kind))
  const lineage = isRecord(signal.lineage) ? signal.lineage : {}
  const sourceUrl = lower([
    text(signal.url),
    text(raw.url),
    text(signal.source_url),
    text(raw.source_url),
    text(signal.source?.url),
    text(signal.source?.source_url),
    text(raw.source?.url),
    text(raw.source?.source_url),
    text(lineage.canonical_url),
    text(lineage.raw_url)
  ].join(" "))

  if (BLOCKED_SOURCE_TYPES.has(sourceType) || signalType === "ai_news") {
    return false
  }
  if (sourceName.includes("openai news") || sourceUrl.includes("openai.com/news") || sourceUrl.includes("openai.com/index")) {
    return false
  }
  if (signalType === "paper" || evidenceType === "paper" || metadataSignalKind === "paper") {
    return true
  }
  if (PAPER_SOURCE_TYPES.has(sourceType) || metadataSourceKind === "paper" || metadataSourceKind === "arxiv") {
    return true
  }
  if (sourceId.includes("arxiv") || sourceUrl.includes("arxiv.org")) {
    return true
  }

  return false
}

function signalToPaper(signal: SourceSignal, index: number): Paper | null {
  const raw = isObject(signal.raw_payload) ? signal.raw_payload : signal
  const title = text(signal.title) || text(raw.title)
  const summary = stripHtml(text(signal.summary) || text(signal.content) || text(raw.summary) || text(raw.raw_content))
  const lineage = isRecord(signal.lineage) ? signal.lineage : {}
  const urls = candidateUrls(signal, raw, lineage)
  const url = urls[0] ?? ""

  if (!title || !summary) {
    return null
  }

  const sourceType = text(signal.source_type) || text(raw.source_type) || text(signal.source?.source_type)
  const sourceName = text(signal.source_name) || text(raw.source_name) || text(signal.source?.source_name)
  const publishedAt = text(signal.published_at) || text(raw.published_at) || text(signal.collected_at) || new Date().toISOString()
  const taskRefs = taskRefsFromRecords(signal, raw)
  const methodRefs = methodRefsFromRecords(signal, raw)
  const pdfUrl = realPdfUrl(signal, raw, urls)
  const repoUrl = realRepoUrl(signal, raw, urls)
  const citationDoi = realCitationDoi(signal, raw, urls)
  const implementations = implementationsFromRecords(signal, raw)
  const benchmarks = benchmarksFromRecords(signal, raw)

  return {
    id: text(signal.signal_id) || text(raw.source_item_id) || `real-paper-${index + 1}`,
    slug: slugify(title) || `real-paper-${index + 1}`,
    title,
    abstractSnippet: summary,
    authors: authors(raw.authors ?? signal.authors, sourceName),
    publishedAt,
    venue: sourceLabel(sourceName, sourceType),
    citationDoi,
    tags: tags(raw.tags ?? signal.tags, sourceType),
    taskRefs,
    methodRefs,
    paperUrl: url,
    arxivUrl: sourceType === "arxiv" || url.includes("arxiv.org") ? url : undefined,
    pdfUrl,
    repoUrl,
    implementations,
    benchmarks,
    sourceRefs: [
      {
        sourceId: text(signal.source_id) || text(raw.source_id),
        sourceName,
        sourceType,
        url,
        title
      }
    ],
    evidenceRefs: [
      {
        evidenceId: text(signal.signal_id) || text(raw.signal_id),
        sourceName,
        sourceType,
        url,
        title,
        summary
      }
    ],
    isPublished: true
  }
}

function dedupeSignals(signals: SourceSignal[]) {
  const seen = new Set<string>()
  const result: SourceSignal[] = []

  for (const signal of signals) {
    const raw = isObject(signal.raw_payload) ? signal.raw_payload : signal
    const key = text(signal.url) || text(raw.url) || text(signal.title) || text(raw.title)
    if (!key || seen.has(key)) {
      continue
    }
    seen.add(key)
    result.push(signal)
  }

  return result
}

function taskRefsFromRecords(...records: SourceSignal[]) {
  return uniqueRefs(records.flatMap((record) => taskRefsFromRecord(record as Record<string, unknown>)))
}

function methodRefsFromRecords(...records: SourceSignal[]) {
  return uniqueRefs(records.flatMap((record) => methodRefsFromRecord(record as Record<string, unknown>)))
}

function taskRefsFromRecord(record: Record<string, unknown>): TaskRef[] {
  return explicitRefs(record, ["taskRefs", "task_refs", "tasks"])
    .map((item) => taskRefFromValue(item))
    .filter((ref): ref is TaskRef => Boolean(ref))
}

function methodRefsFromRecord(record: Record<string, unknown>): MethodRef[] {
  return explicitRefs(record, ["methodRefs", "method_refs", "methods"])
    .map((item) => methodRefFromValue(item))
    .filter((ref): ref is MethodRef => Boolean(ref))
}

function taskRefFromValue(value: unknown): TaskRef | null {
  const record = isRecord(value) ? value : null
  const label = record ? text(record.name) || text(record.label) || text(record.title) : text(value)
  const explicitSlug = record ? text(record.slug) || text(record.taskSlug) || text(record.task_slug) : ""
  const metadata = taskMetadata(explicitSlug || label)
  const slug = canonicalTaskSlug(explicitSlug || metadata?.slug || label)
  if (!slug) {
    return null
  }
  const canonicalMetadata = taskMetadata(slug) ?? metadata
  return {
    id: canonicalMetadata?.id || (record ? text(record.id) || `task-${slug}` : `task-${slug}`),
    slug,
    name: label || canonicalMetadata?.name || titleizeSlug(slug),
    nameZh: cleanLocalizedText(record ? text(record.nameZh) || text(record.name_zh) : undefined) ?? cleanLocalizedText(canonicalMetadata?.nameZh),
    group: (record ? text(record.group) : "") || canonicalMetadata?.group,
    confidence: record ? numberValue(record.confidence) : undefined,
    evidence: record ? text(record.evidence) || undefined : undefined
  }
}

function methodRefFromValue(value: unknown): MethodRef | null {
  const record = isRecord(value) ? value : null
  const label = record ? text(record.name) || text(record.label) || text(record.title) : text(value)
  const explicitSlug = record ? text(record.slug) || text(record.methodSlug) || text(record.method_slug) : ""
  const metadata = methodMetadata(explicitSlug || label)
  const slug = canonicalMethodSlug(explicitSlug || metadata?.slug || label)
  if (!slug) {
    return null
  }
  const canonicalMetadata = methodMetadata(slug) ?? metadata
  return {
    id: canonicalMetadata?.id || (record ? text(record.id) || `method-${slug}` : `method-${slug}`),
    slug,
    name: label || canonicalMetadata?.name || titleizeSlug(slug),
    nameZh: cleanLocalizedText(record ? text(record.nameZh) || text(record.name_zh) : undefined) ?? cleanLocalizedText(canonicalMetadata?.nameZh),
    area: (record ? text(record.area) : "") || canonicalMetadata?.area,
    confidence: record ? numberValue(record.confidence) : undefined,
    evidence: record ? text(record.evidence) || undefined : undefined
  }
}

function explicitRefs(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key]
    if (Array.isArray(value)) {
      return value
    }
  }
  return []
}

function taskMetadata(value: string) {
  const normalized = canonicalTaskSlug(value)
  return paperTasks.find((task) =>
    lower(task.slug) === normalized ||
    lower(task.name) === lower(value) ||
    lower(task.nameZh ?? "") === lower(value)
  )
}

function methodMetadata(value: string) {
  const normalized = canonicalMethodSlug(value)
  return paperMethods.find((method) =>
    lower(method.slug) === normalized ||
    lower(method.name) === lower(value) ||
    lower(method.nameZh ?? "") === lower(value)
  )
}

function implementationsFromRecords(...records: SourceSignal[]) {
  return records.flatMap((record) => implementationsFromRecord(record as Record<string, unknown>))
}

function implementationsFromRecord(record: Record<string, unknown>): PaperImplementation[] {
  return arrayFromKeys(record, ["implementations", "projects", "repositories"])
    .map((item) => implementationFromValue(item))
    .filter((implementation): implementation is PaperImplementation => Boolean(implementation))
}

function implementationFromValue(value: unknown): PaperImplementation | null {
  const record = isRecord(value) ? value : null
  const repoUrl = normalizeGithubRepoUrl(record ? text(record.repoUrl) || text(record.repo_url) || text(record.url) : text(value))
  if (!repoUrl) {
    return null
  }
  const name = record ? text(record.name) || text(record.fullName) || text(record.full_name) : ""
  const repoSlug = githubRepoSlug(repoUrl)
  return {
    id: record ? text(record.id) || repoSlug || repoUrl : repoSlug || repoUrl,
    name: name || repoSlug || repoUrl,
    repoUrl,
    provider: record ? text(record.provider) || "GitHub" : "GitHub",
    githubStars: record ? numberValue(record.githubStars) || numberValue(record.stars) : undefined
  }
}

function benchmarksFromRecords(...records: SourceSignal[]) {
  return records.flatMap((record) => benchmarksFromRecord(record as Record<string, unknown>))
}

function benchmarksFromRecord(record: Record<string, unknown>): PaperBenchmarkResult[] {
  return arrayFromKeys(record, ["benchmarks", "benchmarkResults", "benchmark_results"])
    .map((item) => benchmarkFromValue(item))
    .filter((benchmark): benchmark is PaperBenchmarkResult => Boolean(benchmark))
}

function benchmarkFromValue(value: unknown): PaperBenchmarkResult | null {
  const record = isRecord(value) ? value : null
  const name = record ? text(record.name) || text(record.label) || text(record.title) : text(value)
  if (!name) {
    return null
  }
  const id = record ? text(record.id) || slugify(name) : slugify(name)
  return {
    id,
    name,
    category: record ? text(record.category) || undefined : undefined,
    metric: record ? text(record.metric) || undefined : undefined,
    value: record ? text(record.value) || numberValue(record.value) : undefined,
    taskSlug: record ? text(record.taskSlug) || text(record.task_slug) || undefined : undefined,
    url: record ? text(record.url) || undefined : undefined,
    confidence: record ? numberValue(record.confidence) : undefined,
    evidence: record ? text(record.evidence) || undefined : undefined
  }
}

function arrayFromKeys(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key]
    if (Array.isArray(value)) {
      return value
    }
  }
  return []
}

function uniqueRefs<T extends { slug: string }>(refs: T[]) {
  return refs.filter((ref, index, all) => all.findIndex((candidate) => candidate.slug === ref.slug) === index)
}

function text(value: unknown) {
  return typeof value === "string" ? value.trim() : ""
}

function realPdfUrl(signal: SourceSignal, raw: SourceSignal, urls: string[]) {
  const metadata = isRecord(signal.metadata) ? signal.metadata : {}
  const rawMetadata = isRecord(raw.metadata) ? raw.metadata : {}

  const explicitPdfUrl =
    text((signal as Record<string, unknown>).pdfUrl) ||
    text((signal as Record<string, unknown>).pdf_url) ||
    text((raw as Record<string, unknown>).pdfUrl) ||
    text((raw as Record<string, unknown>).pdf_url) ||
    text(metadata.pdfUrl) ||
    text(metadata.pdf_url) ||
    text(rawMetadata.pdfUrl) ||
    text(rawMetadata.pdf_url)

  return normalizePdfUrl(explicitPdfUrl) ?? urls.map((url) => paperPdfUrlFromSource(url)).find(Boolean)
}

function realRepoUrl(signal: SourceSignal, raw: SourceSignal, urls: string[]) {
  const metadata = isRecord(signal.metadata) ? signal.metadata : {}
  const rawMetadata = isRecord(raw.metadata) ? raw.metadata : {}

  const candidates = uniqueStrings([
    text((signal as Record<string, unknown>).repoUrl),
    text((signal as Record<string, unknown>).repo_url),
    text((signal as Record<string, unknown>).githubUrl),
    text((signal as Record<string, unknown>).github_url),
    text((signal as Record<string, unknown>).codeUrl),
    text((signal as Record<string, unknown>).code_url),
    text((raw as Record<string, unknown>).repoUrl),
    text((raw as Record<string, unknown>).repo_url),
    text((raw as Record<string, unknown>).githubUrl),
    text((raw as Record<string, unknown>).github_url),
    text((raw as Record<string, unknown>).codeUrl),
    text((raw as Record<string, unknown>).code_url),
    text(metadata.repository_url),
    text(metadata.repo_url),
    text(metadata.github_url),
    text(metadata.code_url),
    text(metadata.repository),
    text(rawMetadata.repository_url),
    text(rawMetadata.repo_url),
    text(rawMetadata.github_url),
    text(rawMetadata.code_url),
    text(rawMetadata.repository),
    ...urls
  ].filter(Boolean))

  return candidates.map((value) => normalizeGithubRepoUrl(value)).find(Boolean)
}

function realCitationDoi(signal: SourceSignal, raw: SourceSignal, urls: string[]) {
  const metadata = isRecord(signal.metadata) ? signal.metadata : {}
  const rawMetadata = isRecord(raw.metadata) ? raw.metadata : {}

  const explicitDoi = uniqueStrings([
    text((signal as Record<string, unknown>).doi),
    text((signal as Record<string, unknown>).citationDoi),
    text((signal as Record<string, unknown>).citation_doi),
    text((raw as Record<string, unknown>).doi),
    text((raw as Record<string, unknown>).citationDoi),
    text((raw as Record<string, unknown>).citation_doi),
    text(metadata.doi),
    text(metadata.citationDoi),
    text(metadata.citation_doi),
    text(rawMetadata.doi),
    text(rawMetadata.citationDoi),
    text(rawMetadata.citation_doi)
  ].filter(Boolean)).map((value) => normalizeDoi(value)).find(Boolean)

  if (explicitDoi) {
    return explicitDoi
  }

  const arxivId = uniqueStrings([
    text(metadata.arxiv_id),
    text(rawMetadata.arxiv_id),
    text((signal as Record<string, unknown>).arxivId),
    text((raw as Record<string, unknown>).arxivId),
    ...urls.map((url) => arxivIdFromUrl(url) ?? "")
  ].filter(Boolean)).map((value) => value.replace(/v\d+$/i, "")).find(Boolean)

  return arxivId ? `10.48550/arxiv.${arxivId}` : undefined
}

function candidateUrls(signal: SourceSignal, raw: SourceSignal, lineage: Record<string, unknown>) {
  return uniqueStrings([
    text(signal.url),
    text(raw.url),
    text(signal.source_url),
    text(raw.source_url),
    text(signal.source?.url),
    text(signal.source?.source_url),
    text(raw.source?.url),
    text(raw.source?.source_url),
    text(lineage.canonical_url),
    text(lineage.raw_url),
    ...urlArray(signal.source_urls),
    ...urlArray(raw.source_urls)
  ].filter(Boolean))
}

function urlArray(value: unknown) {
  return Array.isArray(value) ? value.map((item) => text(item)).filter(Boolean) : []
}

function authors(value: unknown, sourceName: string) {
  if (Array.isArray(value)) {
    const names = value.map((item) => text(item)).filter(Boolean)
    if (names.length) {
      return names
    }
  }
  return [sourceName || "Agora Hub Extracted Source"]
}

function cachedAuthors(value: unknown) {
  if (Array.isArray(value)) {
    const names = value.map((item) => text(item)).filter(Boolean)
    if (names.length) {
      return names
    }
  }
  return ["arXiv"]
}

function tags(value: unknown, sourceType: string) {
  const values = Array.isArray(value)
    ? value.map((item) => text(item)).filter(Boolean)
    : text(value).split(/\s+/).filter(Boolean)

  return uniqueStrings([sourceType, ...values].filter(Boolean)).slice(0, 4)
}

function cachedTags(value: unknown) {
  const values = Array.isArray(value)
    ? value.map((item) => text(item)).filter(Boolean)
    : text(value).split(/\s+/).filter(Boolean)

  return uniqueStrings(values).slice(0, 4)
}

function sourceLabel(sourceName: string, sourceType: string) {
  if (sourceName) {
    return sourceName
  }
  if (sourceType === "arxiv") {
    return "arXiv"
  }
  return sourceType || "Extracted"
}

function stripHtml(value: string) {
  return value.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim()
}

function slugify(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "").slice(0, 80)
}

function uniqueStrings(values: string[]) {
  return values.filter((value, index, all) => all.indexOf(value) === index)
}

function latestPaperTimestamp(papers: Paper[]) {
  const timestamps = papers
    .map((paper) => new Date(paper.publishedAt).getTime())
    .filter((value) => Number.isFinite(value))
  if (!timestamps.length) {
    return undefined
  }
  return new Date(Math.max(...timestamps)).toISOString()
}

function numberValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function arraySignals(value: unknown): SourceSignal[] {
  return Array.isArray(value) ? value.filter(isObject) : []
}

function rankedSignals(value: unknown): SourceSignal[] {
  if (!Array.isArray(value)) {
    return []
  }

  return value.map((entry) => (isRecord(entry) ? entry.item : null)).filter(isObject)
}

function lower(value: string) {
  return value.toLowerCase()
}

function isObject(value: unknown): value is SourceSignal {
  return Boolean(value && typeof value === "object")
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object")
}

function isPaper(value: unknown): value is Paper {
  return isRecord(value) && Boolean(text(value.id) && text(value.slug) && text(value.title) && text(value.abstractSnippet))
}
