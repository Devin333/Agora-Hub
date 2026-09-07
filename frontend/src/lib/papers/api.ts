import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api/client"
import type { PaperFeatureFilter } from "@/lib/papers/filters"
import type {
  Locale,
  Paper,
  PaperAISummary,
  PaperDataState,
  PaperListResult,
  PaperMethod,
  PaperPeriod,
  PaperReaderAnswer,
  PaperReaderNote,
  PaperReaderNoteCreate,
  PaperReaderNotePatch,
  PaperReaderPayload,
  PaperRelationGraph,
  PaperSection,
  PaperUserState,
  ReaderEventCreate,
  ReaderEventRecordResult,
  ReaderMaterialSummary,
  ReaderSelectionCreate,
  ReaderSelectionPatch,
  PaperTask,
  RelatedPaper,
  ReadingStatus,
  PaperSort
} from "@/lib/papers/types"

type ApiEnvelope<T> = {
  success: boolean
  data?: T | null
  error?: {
    code: string
    message: string
    details?: unknown
    detail?: unknown
    request_id?: string | null
    requestId?: string
    retryable?: boolean
  } | null
  request_id?: string | null
}

export type PaperListParams = {
  q?: string
  period?: PaperPeriod
  sort?: PaperSort
  limit?: number
  offset?: number
  task?: string
  method?: string
  topic?: string
  from?: string
  to?: string
  has?: string | PaperFeatureFilter[]
}

export type PaperSummaryRequestOptions = {
  refresh?: boolean
  reason?: string
  init?: RequestInit
}

export type PaperTasksResult = {
  tasks: PaperTask[]
  source?: string
  dataState?: PaperDataState
  notices?: string[]
}

export type PaperMethodsResult = {
  methods: PaperMethod[]
  source?: string
  dataState?: PaperDataState
  notices?: string[]
}

export class PapersApiError extends Error {
  code: string
  detail?: unknown
  retryable?: boolean

  constructor(message: string, code = "papers_api_error", detail?: unknown, retryable?: boolean) {
    super(message)
    this.name = "PapersApiError"
    this.code = code
    this.detail = detail
    this.retryable = retryable
  }
}

export async function fetchPapers(params: PaperListParams, init?: RequestInit): Promise<PaperListResult> {
  const envelope = await apiGet<ApiEnvelope<PaperListResult>>(`/api/papers${queryString(params)}`, init)
  return unwrapEnvelope(envelope)
}

export async function fetchPaperDetail(paperId: string, init?: RequestInit): Promise<Paper> {
  const envelope = await apiGet<ApiEnvelope<{ paper: Paper }>>(`/api/papers/${encodeURIComponent(paperId)}`, init)
  return unwrapEnvelope(envelope).paper
}

export async function requestPaperSummary(
  paperId: string,
  locale: Locale,
  options?: RequestInit | PaperSummaryRequestOptions
): Promise<PaperAISummary> {
  const requestOptions = normalizeSummaryRequestOptions(options)
  const searchParams = new URLSearchParams({ locale })
  if (requestOptions.refresh) {
    searchParams.set("refresh", "true")
  }
  const envelope = await apiPost<ApiEnvelope<{ summary: PaperAISummary }>>(
    `/api/papers/${encodeURIComponent(paperId)}/summary?${searchParams.toString()}`,
    requestOptions.refresh ? { reason: requestOptions.reason } : undefined,
    requestOptions.init
  )
  return unwrapEnvelope(envelope).summary
}

export async function refreshPaperSummary(
  paperId: string,
  locale: Locale,
  reason: string,
  init?: RequestInit
): Promise<PaperAISummary> {
  return requestPaperSummary(paperId, locale, {
    refresh: true,
    reason,
    init,
  })
}

export async function fetchPaperReaderPayload(
  paperId: string,
  locale: Locale,
  init?: RequestInit
): Promise<PaperReaderPayload> {
  const envelope = await apiGet<ApiEnvelope<{ reader: PaperReaderPayload }>>(
    `/api/papers/${encodeURIComponent(paperId)}/reader?locale=${locale}`,
    init
  )
  return unwrapEnvelope(envelope).reader
}

export async function fetchPaperSections(
  paperId: string,
  locale: Locale,
  init?: RequestInit
): Promise<PaperSection[]> {
  const envelope = await apiGet<ApiEnvelope<{ sections: PaperSection[] }>>(
    `/api/papers/${encodeURIComponent(paperId)}/sections?locale=${locale}`,
    init
  )
  return unwrapEnvelope(envelope).sections
}

export async function fetchPaperRelated(paperId: string, init?: RequestInit): Promise<RelatedPaper[]> {
  const envelope = await apiGet<ApiEnvelope<{ relatedPapers: RelatedPaper[] }>>(
    `/api/papers/${encodeURIComponent(paperId)}/related`,
    init
  )
  return unwrapEnvelope(envelope).relatedPapers
}

export async function fetchPaperGraph(paperId: string, init?: RequestInit): Promise<PaperRelationGraph> {
  const envelope = await apiGet<ApiEnvelope<{ graph: PaperRelationGraph }>>(
    `/api/papers/${encodeURIComponent(paperId)}/graph`,
    init
  )
  return unwrapEnvelope(envelope).graph
}

export async function fetchPaperTasks(init?: RequestInit): Promise<PaperTask[]> {
  return (await fetchPaperTasksResult(init)).tasks
}

export async function fetchPaperMethods(init?: RequestInit): Promise<PaperMethod[]> {
  return (await fetchPaperMethodsResult(init)).methods
}

export async function fetchPaperTasksResult(init?: RequestInit): Promise<PaperTasksResult> {
  const envelope = await apiGet<ApiEnvelope<PaperTasksResult>>("/api/papers/tasks", init)
  return unwrapEnvelope(envelope)
}

export async function fetchPaperMethodsResult(init?: RequestInit): Promise<PaperMethodsResult> {
  const envelope = await apiGet<ApiEnvelope<PaperMethodsResult>>("/api/papers/methods", init)
  return unwrapEnvelope(envelope)
}

export async function askPaper(
  paperId: string,
  question: string,
  locale: Locale,
  init?: RequestInit
): Promise<PaperReaderAnswer> {
  const envelope = await apiPost<ApiEnvelope<{ answer: PaperReaderAnswer }>>(
    `/api/papers/${encodeURIComponent(paperId)}/ask`,
    { question, locale },
    init
  )
  return unwrapEnvelope(envelope).answer
}

export type PaperUserStatePatch = {
  favorite?: boolean
  subscribed?: boolean
  readingStatus?: ReadingStatus
  currentPage?: number | null
  progressPercent?: number
}

export async function fetchPaperReaderNotes(paperId: string, init?: RequestInit): Promise<PaperReaderNote[]> {
  const envelope = await apiGet<ApiEnvelope<{ notes: PaperReaderNote[] }>>(
    `/api/papers/${encodeURIComponent(paperId)}/notes`,
    init
  )
  return unwrapEnvelope(envelope).notes
}

export async function createPaperReaderNote(
  paperId: string,
  note: PaperReaderNoteCreate,
  init?: RequestInit
): Promise<PaperReaderNote> {
  const envelope = await apiPost<ApiEnvelope<{ note: PaperReaderNote }>>(
    `/api/papers/${encodeURIComponent(paperId)}/notes`,
    note,
    init
  )
  return unwrapEnvelope(envelope).note
}

export async function patchPaperReaderNote(
  paperId: string,
  noteId: string,
  patch: PaperReaderNotePatch,
  init?: RequestInit
): Promise<PaperReaderNote> {
  const envelope = await apiPatch<ApiEnvelope<{ note: PaperReaderNote }>>(
    `/api/papers/${encodeURIComponent(paperId)}/notes/${encodeURIComponent(noteId)}`,
    patch,
    init
  )
  return unwrapEnvelope(envelope).note
}

export async function deletePaperReaderNote(
  paperId: string,
  noteId: string,
  init?: RequestInit
): Promise<boolean> {
  const envelope = await apiDelete<ApiEnvelope<{ deleted: boolean }>>(
    `/api/papers/${encodeURIComponent(paperId)}/notes/${encodeURIComponent(noteId)}`,
    init
  )
  return unwrapEnvelope(envelope).deleted
}

export async function recordReaderEvent(
  paperId: string,
  event: ReaderEventCreate,
  init?: RequestInit
): Promise<ReaderEventRecordResult> {
  const envelope = await apiPost<ApiEnvelope<ReaderEventRecordResult>>(
    `/api/papers/${encodeURIComponent(paperId)}/reader/events`,
    event,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function createReaderSelection(
  paperId: string,
  selection: ReaderSelectionCreate,
  init?: RequestInit
): Promise<ReaderEventRecordResult> {
  const envelope = await apiPost<ApiEnvelope<ReaderEventRecordResult>>(
    `/api/papers/${encodeURIComponent(paperId)}/reader/selections`,
    selection,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function patchReaderSelection(
  paperId: string,
  selectionId: string,
  patch: ReaderSelectionPatch,
  init?: RequestInit
): Promise<ReaderEventRecordResult> {
  const envelope = await apiPatch<ApiEnvelope<ReaderEventRecordResult>>(
    `/api/papers/${encodeURIComponent(paperId)}/reader/selections/${encodeURIComponent(selectionId)}`,
    patch,
    init
  )
  return unwrapEnvelope(envelope)
}

export async function fetchReaderMaterials(paperId: string, init?: RequestInit): Promise<ReaderMaterialSummary> {
  const envelope = await apiGet<ApiEnvelope<{ materials: ReaderMaterialSummary }>>(
    `/api/papers/${encodeURIComponent(paperId)}/reader/materials`,
    init
  )
  return unwrapEnvelope(envelope).materials
}

export async function deleteReaderMaterials(
  paperId: string,
  init?: RequestInit
): Promise<{ eventsDeleted: number; selectionsDeleted: number }> {
  const envelope = await apiDelete<ApiEnvelope<{ deleted: { eventsDeleted: number; selectionsDeleted: number } }>>(
    `/api/papers/${encodeURIComponent(paperId)}/reader/materials`,
    init
  )
  return unwrapEnvelope(envelope).deleted
}

export async function fetchPaperUserState(paperId: string, init?: RequestInit): Promise<PaperUserState> {
  const envelope = await apiGet<ApiEnvelope<{ state: PaperUserState }>>(
    `/api/papers/${encodeURIComponent(paperId)}/state`,
    init
  )
  return unwrapEnvelope(envelope).state
}

export async function patchPaperUserState(
  paperId: string,
  patch: PaperUserStatePatch,
  init?: RequestInit
): Promise<PaperUserState> {
  const envelope = await apiPatch<ApiEnvelope<{ state: PaperUserState }>>(
    `/api/papers/${encodeURIComponent(paperId)}/state`,
    patch,
    init
  )
  return unwrapEnvelope(envelope).state
}

export async function fetchPaperUserStates(paperIds: string[], init?: RequestInit): Promise<PaperUserState[]> {
  const params = new URLSearchParams()
  if (paperIds.length) {
    params.set("paperIds", paperIds.join(","))
  }
  const query = params.toString()
  const envelope = await apiGet<ApiEnvelope<{ states: PaperUserState[] }>>(
    `/api/papers/me/state${query ? `?${query}` : ""}`,
    init
  )
  return unwrapEnvelope(envelope).states
}

function unwrapEnvelope<T>(envelope: ApiEnvelope<T>): T {
  if (envelope.success && envelope.data) {
    return envelope.data
  }
  const error = envelope.error
  throw new PapersApiError(
    error?.message ?? "Papers API request failed",
    error?.code,
    error?.detail ?? error?.details,
    error?.retryable
  )
}

function queryString(params: PaperListParams) {
  const searchParams = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue
    }
    searchParams.set(key, String(value))
  }
  const text = searchParams.toString()
  return text ? `?${text}` : ""
}

function normalizeSummaryRequestOptions(options?: RequestInit | PaperSummaryRequestOptions): PaperSummaryRequestOptions {
  if (!options) {
    return {}
  }
  if ("refresh" in options || "reason" in options || "init" in options) {
    return options as PaperSummaryRequestOptions
  }
  return { init: options as RequestInit }
}
