import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api/client"
import { parseProjectsLabNextAction, parseProjectsLabStage } from "@/types/projects"
import type {
  ProjectClientRequest,
  ProjectDetailResult,
  ProjectItem,
  ProjectListParams,
  ProjectListResult,
  ProjectProductRoute,
  ProjectProductSection,
  ProjectsApiCaseResult,
  ProjectsApiCollection,
  ProjectsApiCollectionResult,
  ProjectsApiHomeResult,
  ProjectsApiListResult,
  ProjectsApiProjectDetail,
  ProjectsApiToolResult,
  ProjectsApiWatchlistResult,
  ProjectsCaseExplainRequest,
  ProjectsCaseExplainResult,
  ProjectsCaseMapRequest,
  ProjectsCaseMapResult,
  ProjectsCollectionCreateRequest,
  ProjectsCollectionGenerateRequest,
  ProjectsCollectionItemCreateRequest,
  ProjectsCollectionMutationResult,
  ProjectsInteractionRequest,
  ProjectsInteractionResponse,
  ProjectsLabAnswerRequest,
  ProjectsLabNodeExplainRequest,
  ProjectsLabNodeExplainResult,
  ProjectsLabSaveRequest,
  ProjectsLabSessionRequest,
  ProjectsLabSession,
  ProjectsLabSessionResponse,
  ProjectsLabSolutionResult,
  ProjectsToolCompareRequest,
  ProjectsToolCompareResult,
  ProjectsToolRecommendRequest,
  ProjectsToolRecommendResult,
  ProjectsWatchlistCreateRequest,
  ProjectsWatchlistDeleteResult,
  ProjectsWatchlistRefreshResult,
  ProjectsWatchlistItemResponse,
  ProjectsWatchlistPatchRequest,
} from "@/types/projects"

type ProjectsLabSessionWire = Omit<
  ProjectsLabSessionResponse["session"],
  "current_stage" | "next_action" | "can_generate_solution" | "unanswered_question_ids"
> & {
  current_stage?: unknown
  next_action?: unknown
  can_generate_solution?: unknown
  unanswered_question_ids?: unknown
}

type ProjectsLabSessionWireResponse = {
  session: ProjectsLabSessionWire
}

type ProjectsLabSolutionWireResult = Omit<ProjectsLabSolutionResult, "session"> & {
  session: ProjectsLabSessionWire
}

type ApiEnvelope<T> = {
  ok?: boolean
  success?: boolean
  data?: T | null
  error?: {
    code: string
    message: string
    detail?: unknown
    details?: unknown
    request_id?: string
    retryable?: boolean
    user_action_required?: boolean
    status?: number
  } | null
}

export class ProjectsApiError extends Error {
  code: string
  detail?: unknown
  retryable?: boolean
  status?: number
  requestId?: string
  userActionRequired?: boolean

  constructor(
    message: string,
    code = "projects_api_error",
    detail?: unknown,
    retryable?: boolean,
    options?: { status?: number; requestId?: string; userActionRequired?: boolean }
  ) {
    super(message)
    this.name = "ProjectsApiError"
    this.code = code
    this.detail = detail
    this.retryable = retryable
    this.status = options?.status
    this.requestId = options?.requestId
    this.userActionRequired = options?.userActionRequired
  }
}

export async function fetchProjects(params: ProjectListParams = {}, init?: RequestInit): Promise<ProjectListResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectListResult>>(`/api/projects${queryString(params)}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectDetail(slug: string, init?: RequestInit): Promise<ProjectItem> {
  const result = await fetchProjectDetailResult(slug, init)
  return result.project
}

export async function fetchProjectDetailResult(slug: string, init?: RequestInit): Promise<ProjectDetailResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectDetailResult>>(`/api/projects/${encodeURIComponent(slug)}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectsHome(params: { limit?: number; user_id?: string } = {}, init?: RequestInit): Promise<ProjectsApiHomeResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiHomeResult>>(`/api/v1/projects${queryString(params)}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectsHot(params: ProjectListParams = {}, init?: RequestInit): Promise<ProjectsApiListResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiListResult>>(`/api/v1/projects/hot${queryString(toApiListParams(params))}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectsRising(params: ProjectListParams = {}, init?: RequestInit): Promise<ProjectsApiListResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiListResult>>(`/api/v1/projects/rising${queryString(toApiListParams(params))}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectsTools(params: ProjectListParams = {}, init?: RequestInit): Promise<ProjectsApiToolResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiToolResult>>(`/api/v1/projects/tools${queryString(toApiListParams(params))}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectsCases(params: ProjectListParams = {}, init?: RequestInit): Promise<ProjectsApiCaseResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiCaseResult>>(`/api/v1/projects/cases${queryString(toApiListParams(params))}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectsCollections(init?: RequestInit): Promise<ProjectsApiCollectionResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiCollectionResult>>("/api/v1/projects/collections", init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectsWatchlist(params: { user_id?: string } = {}, init?: RequestInit): Promise<ProjectsApiWatchlistResult> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiWatchlistResult>>(`/api/v1/projects/watchlist${queryString(params)}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectV1Detail(projectId: string, init?: RequestInit): Promise<ProjectsApiProjectDetail> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiProjectDetail>>(`/api/v1/projects/${encodeURIComponent(projectId)}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectToolDetail(projectId: string, init?: RequestInit): Promise<ProjectsApiToolResult["tools"][number]> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiToolResult["tools"][number]>>(`/api/v1/projects/tools/${encodeURIComponent(projectId)}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectCaseDetail(caseId: string, init?: RequestInit): Promise<ProjectsApiCaseResult["cases"][number]> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiCaseResult["cases"][number]>>(`/api/v1/projects/cases/${encodeURIComponent(caseId)}`, init)
  return unwrapEnvelope(envelope)
}

export async function explainProjectCase(caseId: string, request: ProjectsCaseExplainRequest, init?: RequestInit): Promise<ProjectsCaseExplainResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsCaseExplainResult>>(`/api/v1/projects/cases/${encodeURIComponent(caseId)}/explain`, request, init)
  return unwrapEnvelope(envelope)
}

export async function mapProjectCaseToContext(caseId: string, request: ProjectsCaseMapRequest, init?: RequestInit): Promise<ProjectsCaseMapResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsCaseMapResult>>(`/api/v1/projects/cases/${encodeURIComponent(caseId)}/map-to-context`, request, init)
  return unwrapEnvelope(envelope)
}

export async function fetchProjectCollectionDetail(slug: string, init?: RequestInit): Promise<ProjectsApiCollection> {
  const envelope = await apiGet<ApiEnvelope<ProjectsApiCollection>>(`/api/v1/projects/collections/${encodeURIComponent(slug)}`, init)
  return unwrapEnvelope(envelope)
}

export async function compareProjectTools(request: ProjectsToolCompareRequest, init?: RequestInit): Promise<ProjectsToolCompareResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsToolCompareResult>>("/api/v1/projects/tools/compare", request, init)
  return unwrapEnvelope(envelope)
}

export async function recommendProjectTools(request: ProjectsToolRecommendRequest, init?: RequestInit): Promise<ProjectsToolRecommendResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsToolRecommendResult>>("/api/v1/projects/tools/recommend", request, init)
  return unwrapEnvelope(envelope)
}

export async function startProjectLabSession(request: ProjectsLabSessionRequest, init?: RequestInit): Promise<ProjectsLabSessionResponse> {
  const envelope = await apiPost<ApiEnvelope<ProjectsLabSessionWireResponse>>("/api/v1/projects/lab/sessions", request, init)
  return normalizeLabSessionResponse(unwrapEnvelope(envelope))
}

export async function answerProjectLabQuestion(
  sessionId: string,
  request: ProjectsLabAnswerRequest,
  init?: RequestInit
): Promise<ProjectsLabSessionResponse> {
  const envelope = await apiPost<ApiEnvelope<ProjectsLabSessionWireResponse>>(
    `/api/v1/projects/lab/sessions/${encodeURIComponent(sessionId)}/answer`,
    request,
    init
  )
  return normalizeLabSessionResponse(unwrapEnvelope(envelope))
}

export async function generateProjectLabSolution(sessionId: string, init?: RequestInit): Promise<ProjectsLabSolutionResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsLabSolutionWireResult>>(
    `/api/v1/projects/lab/sessions/${encodeURIComponent(sessionId)}/generate-solution`,
    undefined,
    init
  )
  const result = unwrapEnvelope(envelope)
  return { ...result, session: normalizeLabSession(result.session) }
}

export async function fetchProjectLabSession(sessionId: string, init?: RequestInit): Promise<ProjectsLabSessionResponse> {
  const envelope = await apiGet<ApiEnvelope<ProjectsLabSessionWireResponse>>(`/api/v1/projects/lab/sessions/${encodeURIComponent(sessionId)}`, init)
  return normalizeLabSessionResponse(unwrapEnvelope(envelope))
}

export async function explainProjectLabNode(
  sessionId: string,
  request: ProjectsLabNodeExplainRequest,
  init?: RequestInit
): Promise<ProjectsLabNodeExplainResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsLabNodeExplainResult>>(
    `/api/v1/projects/lab/sessions/${encodeURIComponent(sessionId)}/explain-node`,
    request,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function saveProjectLabSession(
  sessionId: string,
  request: ProjectsLabSaveRequest,
  init?: RequestInit
): Promise<ProjectsLabSessionResponse> {
  const envelope = await apiPost<ApiEnvelope<ProjectsLabSessionWireResponse>>(
    `/api/v1/projects/lab/sessions/${encodeURIComponent(sessionId)}/save`,
    request,
    init
  )
  return normalizeLabSessionResponse(unwrapEnvelope(envelope))
}

export async function createProjectCollection(
  request: ProjectsCollectionCreateRequest,
  init?: RequestInit
): Promise<ProjectsCollectionMutationResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsCollectionMutationResult>>("/api/v1/projects/collections", request, init)
  return unwrapEnvelope(envelope)
}

export async function addProjectCollectionItem(
  collectionId: string,
  request: ProjectsCollectionItemCreateRequest,
  init?: RequestInit
): Promise<ProjectsCollectionMutationResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsCollectionMutationResult>>(
    `/api/v1/projects/collections/${encodeURIComponent(collectionId)}/items`,
    request,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function generateProjectCollection(
  request: ProjectsCollectionGenerateRequest,
  init?: RequestInit
): Promise<ProjectsCollectionMutationResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsCollectionMutationResult>>("/api/v1/projects/collections/generate", request, init)
  return unwrapEnvelope(envelope)
}

export async function addProjectWatchlistItem(
  request: ProjectsWatchlistCreateRequest,
  init?: RequestInit
): Promise<ProjectsWatchlistItemResponse> {
  const envelope = await apiPost<ApiEnvelope<ProjectsWatchlistItemResponse>>("/api/v1/projects/watchlist", request, init)
  return unwrapEnvelope(envelope)
}

export async function patchProjectWatchlistItem(
  itemId: string,
  request: ProjectsWatchlistPatchRequest,
  init?: RequestInit
): Promise<ProjectsWatchlistItemResponse> {
  const envelope = await apiPatch<ApiEnvelope<ProjectsWatchlistItemResponse>>(
    `/api/v1/projects/watchlist/${encodeURIComponent(itemId)}`,
    request,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function deleteProjectWatchlistItem(itemId: string, init?: RequestInit): Promise<ProjectsWatchlistDeleteResult> {
  const envelope = await apiDelete<ApiEnvelope<ProjectsWatchlistDeleteResult>>(
    `/api/v1/projects/watchlist/${encodeURIComponent(itemId)}`,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function refreshProjectWatchlistItem(itemId: string, init?: RequestInit): Promise<ProjectsWatchlistRefreshResult> {
  const envelope = await apiPost<ApiEnvelope<ProjectsWatchlistRefreshResult>>(
    `/api/v1/projects/watchlist/${encodeURIComponent(itemId)}/refresh`,
    undefined,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function recordProjectInteraction(
  request: ProjectsInteractionRequest,
  init?: RequestInit
): Promise<ProjectsInteractionResponse> {
  const envelope = await apiPost<ApiEnvelope<ProjectsInteractionResponse>>("/api/v1/projects/interactions", request, init)
  return unwrapEnvelope(envelope)
}

export const PROJECT_PRODUCT_SECTIONS: ProjectProductSection[] = [
  {
    id: "hot",
    title: "Hot Projects",
    description: "Projects ranked by external heat, internal behavior, technical relevance, freshness, and source trust.",
    href: "/projects/hot",
    params: { sort: "trending", limit: 18 },
  },
  {
    id: "rising",
    title: "Rising Projects",
    description: "Projects ranked by velocity, novelty, update cadence, early quality, and attention growth.",
    href: "/projects/rising",
    params: { sort: "growth", limit: 18 },
  },
  {
    id: "tools",
    title: "Tools",
    description: "Real Project Radar tools grouped by capability, integration surface, and deployment fit.",
    href: "/projects/tools",
    params: { sort: "activity", limit: 18 },
  },
  {
    id: "cases",
    title: "Cases",
    description: "Module cases derived from real projects, capabilities, and public source references.",
    href: "/projects/cases",
    params: { sort: "quality", limit: 18 },
  },
  {
    id: "lab",
    title: "Lab",
    description: "A design lab that starts from your requirement profile and real-derived project cases.",
    href: "/projects/lab",
    params: { sort: "newest", limit: 18 },
  },
  {
    id: "collections",
    title: "Collections",
    description: "Topic collections generated from real Project Radar projects without synthetic filler.",
    href: "/projects/collections",
    params: { sort: "trending", limit: 48 },
  },
  {
    id: "watchlist",
    title: "Watchlist",
    description: "Projects you are tracking, backed by local Projects state and real project identifiers.",
    href: "/projects/watchlist",
    params: { sort: "quality", limit: 24 },
  },
]

export function projectProductSection(route: ProjectProductRoute): ProjectProductSection {
  if (route === "home") {
    return {
      id: "home",
      title: "Projects",
      description: "A productized Projects home built on real Project Radar artifacts.",
      href: "/projects",
      params: { sort: "trending", limit: 24 },
    }
  }
  return PROJECT_PRODUCT_SECTIONS.find((section) => section.id === route) ?? PROJECT_PRODUCT_SECTIONS[0]
}

export async function fetchProjectProductSection(
  route: ProjectProductRoute,
  request: ProjectClientRequest = {}
): Promise<ProjectsApiHomeResult | ProjectsApiListResult | ProjectsApiToolResult | ProjectsApiCaseResult | ProjectsApiCollectionResult | ProjectsApiWatchlistResult> {
  const params = { ...projectProductSection(route).params, ...request.params }
  if (route === "home") return fetchProjectsHome({ limit: params.limit }, request.init)
  if (route === "hot") return fetchProjectsHot(params, request.init)
  if (route === "rising") return fetchProjectsRising(params, request.init)
  if (route === "tools") return fetchProjectsTools(params, request.init)
  if (route === "cases") return fetchProjectsCases(params, request.init)
  if (route === "collections") return fetchProjectsCollections(request.init)
  if (route === "watchlist") return fetchProjectsWatchlist({}, request.init)
  return fetchProjectsHome({ limit: params.limit }, request.init)
}

function unwrapEnvelope<T>(envelope: ApiEnvelope<T>): T {
  if ((envelope.success || envelope.ok) && envelope.data) {
    return envelope.data
  }
  const error = envelope.error
  throw new ProjectsApiError(
    error?.message ?? "Projects API request failed",
    error?.code,
    error?.detail ?? error?.details,
    error?.retryable,
    {
      status: error?.status,
      requestId: error?.request_id,
      userActionRequired: error?.user_action_required,
    }
  )
}

function normalizeLabSessionResponse(response: ProjectsLabSessionWireResponse): ProjectsLabSessionResponse {
  return { ...response, session: normalizeLabSession(response.session) }
}

function normalizeLabSession(session: ProjectsLabSessionWire): ProjectsLabSession {
  const parsedStage = parseProjectsLabStage(session.current_stage)
  const nextAction = parseProjectsLabNextAction(session.next_action)
  const questions = Array.isArray(session.questions) ? session.questions : []
  const unanswered = Array.isArray(session.unanswered_question_ids)
    ? session.unanswered_question_ids.filter((value): value is string => typeof value === "string")
    : questions
        .filter((question) => question.required !== false && (question.answered_value === undefined || question.answered_value === null || question.answered_value === ""))
        .map((question) => question.id)
  return {
    ...session,
    current_stage: parsedStage.value,
    raw_current_stage: parsedStage.raw,
    next_action: nextAction,
    can_generate_solution: session.can_generate_solution === true,
    unanswered_question_ids: unanswered,
  } as ProjectsLabSession
}

function queryString(params: Record<string, unknown>): string {
  const searchParams = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue
    searchParams.set(key, String(value))
  }
  const text = searchParams.toString()
  return text ? `?${text}` : ""
}

function toApiListParams(params: ProjectListParams): Record<string, unknown> {
  return {
    q: params.q,
    category: params.category,
    tag: params.topic,
    source: params.source,
    license: params.license,
    sort: params.sort,
    page: params.page,
    page_size: params.pageSize,
    limit: params.limit,
  }
}
