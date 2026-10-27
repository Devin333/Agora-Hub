import type { LucideIcon } from "lucide-react"
import type { ReactNode } from "react"

export type StudioApiError = {
  code: string
  message: string
  details?: unknown
  retryable?: boolean
  userActionRequired?: boolean
  requestId?: string
  status?: number
}

export type StudioModuleStatus = "ready" | "partial" | "fallback"

export type StudioNavigationItem = {
  label: string
  href: string
  description?: string
  icon?: LucideIcon
  status?: StudioModuleStatus
}

export type StudioNavigationGroup = {
  label: string
  items: StudioNavigationItem[]
}

export type StudioModuleEntry = {
  title: string
  description: string
  href: string
  coreObject: string
  targetApi: string
  status: StudioModuleStatus
  icon?: LucideIcon
  actionLabel?: string
}

export type StudioPageStateKind = "loading" | "empty" | "error"

export type StudioPageStateAction = {
  label: string
  onClick?: () => void
  href?: string
}

export type StudioPageState = {
  kind: StudioPageStateKind
  title: string
  description?: string
  action?: StudioPageStateAction
}

export type StudioFallbackNotice = {
  title?: string
  message: string
  requestId?: string
  error?: StudioApiError
  action?: ReactNode
}

export type PaperPdfProxyDataState = "ready" | "partial" | "empty" | "fallback"

export type PaperPdfProxyHostStats = {
  host: string
  requestCount: number
  errorCount: number
  avgDurationMs?: number
}

export type PaperPdfProxyRecentError = {
  timestamp: string
  host?: string
  path?: string
  code: string
  status?: number
  durationMs?: number
}

export type PaperPdfProxyStats = {
  dataState: PaperPdfProxyDataState
  windowHours: number
  generatedAt: string
  windowStartedAt: string
  windowEndedAt: string
  totalRequests: number
  successCount: number
  errorCount: number
  timeoutCount: number
  oversizedCount: number
  blockedCount: number
  invalidContentTypeCount: number
  upstreamFailureCount: number
  errorsByCode: Record<string, number>
  topHosts: PaperPdfProxyHostStats[]
  recentErrors: PaperPdfProxyRecentError[]
  notices: string[]
}

export type PaperReaderOpsDataState = "ready" | "partial" | "empty" | "fallback"

export type PaperReaderRuntimeFileStatus = "ready" | "partial" | "empty" | "missing" | "invalid"

export type PaperReaderCacheStats = {
  status: PaperReaderRuntimeFileStatus
  exists: boolean
  paperCount: number
  collectedAt?: string | null
  source?: string | null
  lastUpdatedAt?: string | null
}

export type PaperSummaryCacheStats = {
  status: PaperReaderRuntimeFileStatus
  exists: boolean
  entryCount: number
  v2EntryCount: number
  localeCounts: Record<string, number>
  modelRouteCounts: Record<string, number>
  lastGeneratedAt?: string | null
  lastUpdatedAt?: string | null
}

export type PaperSummaryRecentFailure = {
  timestamp: string
  paperId?: string
  locale?: string
  modelRoute?: string
  errorCode?: string
  durationMs?: number
  schemaVersion?: string
}

export type PaperSummaryEventStats = {
  status: PaperReaderRuntimeFileStatus
  exists: boolean
  eventCount: number
  cacheHitCount: number
  generatedCount: number
  failureCount: number
  hitRate: number
  outcomeCounts: Record<string, number>
  errorCodeCounts: Record<string, number>
  localeCounts: Record<string, number>
  modelRouteCounts: Record<string, number>
  recentFailures: PaperSummaryRecentFailure[]
  averageDurationMs: number
  lastUpdatedAt?: string | null
}

export type PaperReaderArtifactStats = {
  status: PaperReaderRuntimeFileStatus
  exists: boolean
  fileCount: number
  lastUpdatedAt?: string | null
}

export type PaperReaderOpsStats = {
  dataState: PaperReaderOpsDataState
  windowHours: number
  windowStart: string
  windowEnd: string
  paperCache: PaperReaderCacheStats
  summaryCache: PaperSummaryCacheStats
  summaryEvents: PaperSummaryEventStats
  readerCache: PaperReaderArtifactStats
  textExtraction: PaperReaderArtifactStats
  lastUpdatedAt?: string | null
}

export type PaperIngestRun = {
  runId: string
  status: string
  startedAt: string
  finishedAt?: string | null
  candidateLimit: number
  minGithubStars: number
  autoTaxonomyConfidence?: number
  candidateCount: number
  processedCount: number
  publishedCount: number
  skippedNoGithubCount: number
  skippedLowStarsCount: number
  repairQueuedCount: number
  blockedCount: number
  failureCount: number
  publishedPaperIds: string[]
  errors?: Array<Record<string, unknown>>
}

export type PaperIngestRepairItem = {
  itemId: string
  runId: string
  paperId?: string
  title?: string
  step: string
  errorCode: string
  errorMessage: string
  status: string
  queue: "agent_repair" | "manual_blocked" | string
  repairAction?: string
  retryAt?: string
  createdAt: string
  userActionRequired?: boolean
}

export type PaperIngestTaxonomyEvent = {
  eventId: string
  runId: string
  paperId: string
  kind: "task" | "method" | "benchmark" | string
  slug: string
  name: string
  confidence?: number
  action: string
  createdAt: string
}

export type PaperIngestOpsState = {
  runs: PaperIngestRun[]
  repairQueue: PaperIngestRepairItem[]
  blockedItems: PaperIngestRepairItem[]
  taxonomyEvents: PaperIngestTaxonomyEvent[]
  promptMemory: Array<Record<string, unknown>>
  config: {
    candidateLimit: number
    minGithubStars: number
    autoTaxonomyConfidence: number
    arxivQuery: string
    classifierModelRoute: string
  }
}

export type PaperIngestTriggerResult = {
  message_id: string
  task_id: string
  task_type: string
  queue_name: string
  status: string
  run_id?: string
  mode?: "worker_queue" | "local_background" | string
  fallback_reason?: string
}

export type PaperVisualCompileBackfillTriggerResult = {
  message_id: string
  task_id: string
  task_type: string
  queue_name: string
  status: string
  run_id?: string
  force?: boolean
  limit?: number | null
  mode?: "worker_queue" | "local_background" | string
  fallback_reason?: string
}
