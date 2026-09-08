export type ProjectCategory =
  | "agent_framework"
  | "rag"
  | "llm_infra"
  | "inference"
  | "evaluation"
  | "coding"
  | "multimodal"
  | "data"
  | "memory"
  | "workflow"

export type ProjectCategoryAlias =
  | ProjectCategory
  | "agent"
  | "devtool"
  | "framework"
  | "infra"
  | "llm"
  | "dataset"

export type ProjectMaturity = "new" | "rising" | "active" | "mature" | "dormant" | "experimental"

export type ProjectSource = "github" | "huggingface" | "paper" | "manual"

export type ProjectLanguage = "python" | "typescript" | "rust" | "go" | "other"

export type ProjectPeriod = "daily" | "weekly" | "monthly" | "all"

export type ProjectSort = "trending" | "newest" | "stars" | "activity" | "growth" | "quality"

export type ProjectDataState = "ready" | "partial" | "empty"

export type ProjectProductRoute =
  | "home"
  | "hot"
  | "rising"
  | "tools"
  | "cases"
  | "lab"
  | "collections"
  | "watchlist"

export type ProjectProductSection = {
  id: ProjectProductRoute
  title: string
  description: string
  href: string
  params: ProjectListParams
}

export type ProjectScores = {
  trendScore?: number
  starVelocityScore?: number
  freshnessScore?: number
  activityScore?: number
  adoptionScore?: number
  evidenceScore?: number
  qualityScore?: number
}

export type EvidenceRef = {
  id?: string
  title?: string
  sourceName?: string
  sourceType?: string
  sourceUrl?: string
  url?: string
  collectedAt?: string
  publishedAt?: string
  reliability?: string
  summary?: string
}

export type ProjectCategoryRef = {
  category: ProjectCategory
  label: string
  confidence?: number
}

export type RelatedPaperRef = {
  id?: string
  title: string
  url?: string
  summary?: string
}

export type RelatedNewsRef = {
  id?: string
  title: string
  url?: string
  sourceName?: string
  publishedAt?: string
}

export type RelatedCommunityRef = {
  id?: string
  title: string
  url?: string
  sourceName?: string
  publishedAt?: string
}

export type ProjectRelationCounts = {
  papers: number
  news: number
  community: number
}

export type ProjectItem = {
  id: string
  slug: string
  name: string
  fullName: string
  description: string
  repoUrl: string
  homepageUrl?: string
  owner?: string
  language?: string
  license?: string
  stars?: number
  forks?: number
  watchers?: number
  openIssues?: number
  starGrowth24h?: number
  starGrowth7d?: number
  projectMomentum?: number
  qualityScore?: number
  scores: ProjectScores
  categoryRefs: ProjectCategoryRef[]
  categories: ProjectCategory[]
  tags: string[]
  topics: string[]
  maturity?: ProjectMaturity
  createdAt?: string
  updatedAt?: string
  pushedAt?: string
  lastPushedAt?: string
  firstSeenAt?: string
  sourceRefs?: EvidenceRef[]
  relatedPapers?: RelatedPaperRef[]
  relatedNews?: RelatedNewsRef[]
  relatedCommunityTopics?: RelatedCommunityRef[]
  relationCounts: ProjectRelationCounts
  problemSolved?: string
  whyItMatters?: string
  sources?: ProjectSource[]
}

export type ProjectListParams = {
  q?: string
  category?: ProjectCategoryAlias
  topic?: string
  sort?: ProjectSort
  source?: ProjectSource
  language?: ProjectLanguage
  license?: "MIT" | "Apache-2.0" | "BSD-3-Clause"
  maturity?: ProjectMaturity
  period?: ProjectPeriod
  page?: number
  pageSize?: number
  limit?: number
  cursor?: string
}

export type ProjectClientRequest = {
  params?: ProjectListParams
  init?: RequestInit
}

export type ProjectsApiSource = "backend" | "artifact" | "none"

export type ProjectsApiDataState = "ready" | "partial" | "empty"

export type ProjectsApiMeta = {
  source: ProjectsApiSource
  source_run_id?: string | null
  generated_at?: string | null
  data_state: ProjectsApiDataState
  notices: string[]
}

export type ProjectsApiMetric = {
  label: string
  value: string | number
  hint?: string | null
}

export type ProjectsApiPageInfo = {
  page: number
  page_size: number
  total: number
  has_next: boolean
  next_cursor?: string | null
}

export type ProjectsApiProject = {
  id: string
  slug: string
  name: string
  tagline?: string | null
  description?: string | null
  canonical_url?: string | null
  website_url?: string | null
  github_url?: string | null
  docs_url?: string | null
  demo_url?: string | null
  project_type: string
  category?: string | null
  tags: string[]
  source_confidence: number
  hot_score?: number | null
  rising_score?: number | null
  rank?: number | null
  rank_reason?: string | null
  metric_summary: Record<string, unknown>
  capability_count: number
  case_count: number
  source_count: number
  updated_at?: string | null
}

export type ProjectsApiListResult = {
  items: ProjectsApiProject[]
  page: ProjectsApiPageInfo
  meta: ProjectsApiMeta
  metrics: ProjectsApiMetric[]
}

export type ProjectsApiProjectDetail = {
  project: ProjectsApiProject
  sources: Array<Record<string, unknown>>
  metrics: Array<Record<string, unknown>>
  growth: Array<Record<string, unknown>>
  capabilities: Array<Record<string, unknown>>
  tool_profile?: ProjectsApiTool["profile"] | null
  cases: ProjectsApiCase[]
  collections?: ProjectsApiCollection[]
  watch_status?: ProjectsApiWatchlistItem | null
  recommended_actions?: Array<Record<string, unknown>>
  ranking?: Record<string, unknown>
  meta: ProjectsApiMeta
}

export type ProjectsApiTool = {
  project: ProjectsApiProject
  profile: {
    project_id: string
    tool_type: string
    input_types: string[]
    output_types: string[]
    is_open_source?: boolean | null
    license?: string | null
    local_deployable?: boolean | null
    has_api?: boolean | null
    has_cli?: boolean | null
    has_python_sdk?: boolean | null
    has_docker?: boolean | null
    integration_difficulty: "low" | "medium" | "high"
    recommended_integration?: "direct_use" | "wrap_as_service" | "reference_only" | null
    target_modules: string[]
    setup_commands: string[]
    usage_example?: string | null
    known_limits: string[]
    experiment_status: "untested" | "runnable" | "failed" | "adopted"
  }
  capabilities: Array<Record<string, unknown>>
  fit_reason?: string | null
}

export type ProjectsApiToolResult = {
  tools: ProjectsApiTool[]
  page: ProjectsApiPageInfo
  meta: ProjectsApiMeta
}

export type ProjectsApiCase = Record<string, unknown> & {
  id: string
  project_id: string
  title: string
  business_domain: string
  module_type: string
  problem?: string
  design_summary?: string
}

export type ProjectsApiCaseResult = {
  cases: ProjectsApiCase[]
  page: ProjectsApiPageInfo
  meta: ProjectsApiMeta
}

export type ProjectsCaseExplainRequest = {
  style?: "plain" | "technical" | "migration"
  user_context?: string | null
}

export type ProjectsCaseExplainResult = {
  case_id: string
  style: "plain" | "technical" | "migration"
  summary: string
  key_points: string[]
  component_explanations: Array<Record<string, unknown>>
  pattern_explanations: Array<Record<string, unknown>>
  migration_notes: string[]
  source_refs: string[]
}

export type ProjectsCaseMapRequest = {
  user_context: string
  target_module?: string | null
  constraints?: string[]
}

export type ProjectsCaseMapResult = {
  case_id: string
  fit_score: number
  reusable_components: Array<Record<string, unknown>>
  migration_steps: string[]
  cautions: string[]
  source_refs: string[]
}

export type ProjectsApiCollection = Record<string, unknown> & {
  id: string
  slug: string
  title: string
  description: string
  item_count?: number
}

export type ProjectsApiCollectionResult = {
  collections: ProjectsApiCollection[]
  meta: ProjectsApiMeta
}

export type ProjectsCollectionMutationResult = {
  collection: ProjectsApiCollection
  meta: ProjectsApiMeta
}

export type ProjectsCollectionCreateRequest = {
  title: string
  description: string
  collection_type?: string
  tags?: string[]
  target_audience?: string[]
  learning_goals?: string[]
}

export type ProjectsCollectionItemCreateRequest = {
  item_type: "project" | "tool" | "case" | "pattern" | "external_link"
  item_id?: string | null
  external_url?: string | null
  title: string
  reason: string
  order?: number | null
  difficulty?: string | null
  recommended_action?: string | null
}

export type ProjectsCollectionGenerateRequest = {
  topic: string
  project_ids?: string[]
  case_ids?: string[]
  collection_type?: string
}

export type ProjectsApiWatchlistItem = Record<string, unknown> & {
  id: string
  project_id: string
  watch_reason: string
  priority: "low" | "medium" | "high"
  status: "active" | "paused" | "archived"
}

export type ProjectsApiWatchlistResult = {
  items: ProjectsApiWatchlistItem[]
  meta: ProjectsApiMeta
}

export type ProjectsToolCompareRequest = {
  project_ids: string[]
}

export type ProjectsToolCompareResult = {
  tools: ProjectsApiTool[]
  matrix: Array<Record<string, unknown>>
  recommendation?: string | null
  meta: ProjectsApiMeta
}

export type ProjectsToolRecommendRequest = {
  problem: string
  target_module?: string | null
  input_type?: string | null
  output_type?: string | null
  deployment?: string | null
  max_difficulty?: "low" | "medium" | "high" | null
  limit?: number
}

export type ProjectsToolRecommendResult = {
  tools: ProjectsApiTool[]
  reasoning: string[]
  meta: ProjectsApiMeta
}

export const PROJECTS_LAB_STAGES = [
  "clarifying_requirements",
  "ready_to_generate",
  "solution_generated",
  "solution_saved",
  "solution_adopted",
  "solution_archived",
] as const

export type ProjectsLabStage = (typeof PROJECTS_LAB_STAGES)[number]
export type ProjectsLabStageValue = ProjectsLabStage | "unknown"

export const PROJECTS_LAB_NEXT_ACTIONS = [
  "answer_question",
  "generate_solution",
  "review_solution",
  "save_solution",
  "none",
] as const

export type ProjectsLabNextAction = (typeof PROJECTS_LAB_NEXT_ACTIONS)[number]
export type ProjectsLabNextActionValue = ProjectsLabNextAction | "unknown"

export type ProjectsLabStageParseResult = {
  value: ProjectsLabStageValue
  raw: string | null
  known: boolean
}

export function parseProjectsLabStage(value: unknown): ProjectsLabStageParseResult {
  const raw = typeof value === "string" ? value : value == null ? null : String(value)
  if (raw && (PROJECTS_LAB_STAGES as readonly string[]).includes(raw)) {
    return { value: raw as ProjectsLabStage, raw, known: true }
  }
  return { value: "unknown", raw, known: false }
}

export function parseProjectsLabNextAction(value: unknown): ProjectsLabNextActionValue {
  if (typeof value === "string" && (PROJECTS_LAB_NEXT_ACTIONS as readonly string[]).includes(value)) {
    return value as ProjectsLabNextAction
  }
  return "unknown"
}

export type ProjectsLabSessionRequest = {
  user_problem: string
  user_id?: string | null
  business_domain?: string | null
  module_type?: string | null
  target_goal?: string | null
  current_project_context?: string | null
  selected_case_ids?: string[]
}

export type ProjectsLabAnswerRequest = {
  question_id: string
  answer: unknown
}

export type ProjectsLabSession = Record<string, unknown> & {
  id: string
  user_problem: string
  selected_case_ids: string[]
  questions: Array<Record<string, unknown> & {
    id: string
    question: string
    options?: Array<{ label: string; value: string }>
    required?: boolean
    answered_value?: unknown
  }>
  current_stage: ProjectsLabStageValue
  raw_current_stage?: string | null
  next_action: ProjectsLabNextActionValue
  can_generate_solution: boolean
  unanswered_question_ids: string[]
  generated_solution?: string | null
  solution_json?: Record<string, unknown> | null
}

export type ProjectsLabSessionResponse = {
  session: ProjectsLabSession
}

export type ProjectsLabSolutionResult = {
  session: ProjectsLabSession
  solution: Record<string, unknown>
}

export type ProjectsLabNodeExplainRequest = {
  node_id: string
  style?: "plain" | "technical"
}

export type ProjectsLabNodeExplainResult = {
  session_id: string
  node_id: string
  title: string
  explanation: string
  related_nodes: Array<Record<string, unknown>>
}

export type ProjectsLabSaveRequest = {
  status?: "saved" | "adopted" | "archived"
  note?: string | null
}

export type ProjectsWatchlistCreateRequest = {
  project_id: string
  user_id?: string | null
  watch_reason: string
  watch_topics?: string[]
  priority?: "low" | "medium" | "high"
  notify_on?: string[]
}

export type ProjectsWatchlistPatchRequest = {
  watch_reason?: string | null
  watch_topics?: string[] | null
  priority?: "low" | "medium" | "high" | null
  status?: "active" | "paused" | "archived" | null
  notify_on?: string[] | null
  next_action?: string | null
}

export type ProjectsWatchlistItemResponse = {
  item: ProjectsApiWatchlistItem
}

export type ProjectsWatchlistDeleteResult = {
  deleted: boolean
  item_id: string
}

export type ProjectsWatchlistRefreshResult = {
  item: ProjectsApiWatchlistItem
  signals: Array<Record<string, unknown>>
  meta: ProjectsApiMeta
}

export type ProjectsInteractionRequest = {
  event_type: string
  target_type: "project" | "tool" | "case" | "component" | "collection" | "lab_session" | "solution" | "watchlist"
  target_id?: string | null
  user_id?: string | null
  session_id?: string | null
  query_text?: string | null
  action_value?: string | null
  signal_strength?: number
  metadata?: Record<string, unknown>
}

export type ProjectsInteractionResponse = {
  event: Record<string, unknown> & { id: string }
}

export type ProjectsApiHomeResult = {
  hot: ProjectsApiProject[]
  rising: ProjectsApiProject[]
  tools: ProjectsApiProject[]
  cases: ProjectsApiCase[]
  collections: ProjectsApiCollection[]
  watchlist: ProjectsApiWatchlistItem[]
  recommendations: Array<Record<string, unknown>>
  meta: ProjectsApiMeta
  metrics: ProjectsApiMetric[]
}

export type ProjectMetric = {
  label: string
  value: string | number
  hint?: string
}

export type ProjectFilterOption = {
  value: string
  label: string
  count: number
}

export type ProjectListOptions = {
  categories: ProjectFilterOption[]
  sources: ProjectFilterOption[]
  languages: ProjectFilterOption[]
  topics: ProjectFilterOption[]
  maturity: ProjectFilterOption[]
}

export type ProjectPageInfo = {
  page: number
  pageSize: number
  total: number
  hasNext: boolean
  nextCursor?: string | null
}

export type ProjectListResult = {
  items: ProjectItem[]
  allItems: ProjectItem[]
  allFiltered: ProjectItem[]
  metrics: ProjectMetric[]
  options: ProjectListOptions
  page: ProjectPageInfo
  dataState: ProjectDataState
  source: "backend" | "artifact" | "none"
  sourceRunId?: string
  generatedAt?: string
  notices: string[]
}

export type ProjectDetailResult = {
  project: ProjectItem
  dataState: ProjectDataState
  source: "backend" | "artifact"
  sourceRunId?: string
  generatedAt?: string
  notices: string[]
}
