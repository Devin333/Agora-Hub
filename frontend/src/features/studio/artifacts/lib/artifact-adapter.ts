import type {
  ArtifactDataState,
  ArtifactPreviewKind,
  StudioArtifact,
  StudioArtifactRunDetail,
  StudioArtifactRunSummary,
  StudioLineageRef,
  StudioReplayBundle
} from "@/types/artifact"

export const ARTIFACT_PREVIEW_LIMIT_BYTES = 100 * 1024

export type ApiRunList = {
  run_count?: number
  runs?: ApiRunListItem[]
}

export type ApiRunListItem = {
  run_id?: string | null
  id?: string | null
  workflow_id?: string | null
  workflow_version?: string | null
  profile?: string | null
  status?: string | null
  started_at?: string | null
  finished_at?: string | null
  manifest_path?: string | null
  event_count?: number | null
  artifact_count?: number | null
  step_count?: number | null
}

export type ApiRunDetail = ApiRunListItem & {
  manifest?: Record<string, unknown> | null
  output_preview?: Record<string, unknown> | null
  metrics?: Record<string, unknown> | null
}

export type ApiArtifact = {
  artifact_key?: string | null
  artifact_id?: string | null
  relative_path?: string | null
  path?: string | null
  content_type?: string | null
  size_bytes?: number | null
  content?: unknown
  read_error?: string | null
}

export type ApiArtifactList = {
  run_id?: string | null
  artifact_count?: number | null
  artifacts?: ApiArtifact[]
}

export type ApiReplayBundle = {
  run_id?: string | null
  manifest?: Record<string, unknown> | null
  manifest_path?: string | null
  event_count?: number | null
  events?: Array<Record<string, unknown>>
  events_path?: string | null
  events_error?: string | null
  artifact_count?: number | null
  artifacts?: ApiArtifact[]
  step_result_count?: number | null
  step_results?: Record<string, unknown> | null
  integrity?: Record<string, unknown> | null
}

export type ApiLineageRef = {
  lineage_id?: string | null
  run_id?: string | null
  source_type?: string | null
  source_id?: string | null
  target_type?: string | null
  target_id?: string | null
  relation_type?: string | null
  created_at?: string | null
}

export type ApiLineageResult = {
  run_id?: string | null
  query_type?: string | null
  lineage_count?: number | null
  lineage_refs?: ApiLineageRef[]
}

export const fallbackReplayBundle: ApiReplayBundle = {
  run_id: "fallback-run-artifact-replay",
  manifest_path: ".newsroom/runs/fallback-run-artifact-replay/manifest.json",
  manifest: {
    run_id: "fallback-run-artifact-replay",
    workflow_id: "daily-newsroom-runtime",
    status: "succeeded",
    artifacts: {
      manifest: "manifest.json",
      events: "events.jsonl",
      step_results: "step_results.json",
      report_markdown: "report.md",
      report_json: "report.json",
      source_html: "source.html"
    }
  },
  event_count: 3,
  events: [
    { event_type: "workflow_started", step_id: "collect", payload: { profile: "fallback" } },
    { event_type: "artifact_written", step_id: "report", payload: { artifact_key: "report_markdown" } },
    { event_type: "workflow_finished", step_id: "final", payload: { status: "succeeded" } }
  ],
  events_path: ".newsroom/runs/fallback-run-artifact-replay/events.jsonl",
  artifact_count: 5,
  artifacts: [
    {
      artifact_key: "manifest",
      relative_path: "manifest.json",
      content_type: "application/json",
      size_bytes: 486,
      content: {
        run_id: "fallback-run-artifact-replay",
        workflow_id: "daily-newsroom-runtime",
        status: "succeeded"
      }
    },
    {
      artifact_key: "report_markdown",
      relative_path: "report.md",
      content_type: "text/markdown",
      size_bytes: 128,
      content: "# 运行复盘\n\nFallback 数据展示 replay bundle 的基础结构。"
    },
    {
      artifact_key: "report_json",
      relative_path: "report.json",
      content_type: "application/json",
      size_bytes: 120,
      content: { title: "Fallback report", secret: "[redacted]" }
    },
    {
      artifact_key: "source_html",
      relative_path: "source.html",
      content_type: "text/html",
      size_bytes: 88,
      content: "<article><h1>Escaped HTML</h1><script>window.bad = true</script></article>"
    },
    {
      artifact_key: "dataset_snapshot",
      relative_path: "dataset.parquet",
      content_type: "application/octet-stream",
      size_bytes: 262144
    }
  ],
  step_result_count: 2,
  step_results: {
    collect: { status: "succeeded", outputs: { item_count: 24 } },
    report: { status: "succeeded", outputs: { artifact_key: "report_markdown" } }
  },
  integrity: {
    valid: true,
    missing_artifact_files: [],
    checksum_mismatches: []
  }
}

export const fallbackLineage: ApiLineageResult = {
  run_id: "fallback-run-artifact-replay",
  query_type: "list",
  lineage_count: 2,
  lineage_refs: [
    {
      lineage_id: "lin_fallback_source_evidence",
      run_id: "fallback-run-artifact-replay",
      source_type: "source_item",
      source_id: "openai-blog:item-001",
      target_type: "evidence",
      target_id: "ev-fallback-001",
      relation_type: "source_to_evidence",
      created_at: "2026-05-23T08:12:00Z"
    },
    {
      lineage_id: "lin_fallback_evidence_report",
      run_id: "fallback-run-artifact-replay",
      source_type: "evidence",
      source_id: "ev-fallback-001",
      target_type: "report",
      target_id: "report-fallback-001",
      relation_type: "evidence_to_report",
      created_at: "2026-05-23T08:18:00Z"
    }
  ]
}

export const fallbackRuns: StudioArtifactRunSummary[] = [
  mapRunSummary(
    {
      run_id: "fallback-run-artifact-replay",
      workflow_id: "daily-newsroom-runtime",
      workflow_version: "fallback",
      profile: "studio-fallback",
      status: "succeeded",
      started_at: "2026-05-23T08:12:00Z",
      finished_at: "2026-05-23T08:31:00Z",
      manifest_path: ".newsroom/runs/fallback-run-artifact-replay/manifest.json",
      event_count: fallbackReplayBundle.event_count,
      artifact_count: fallbackReplayBundle.artifact_count
    },
    fallbackReplayBundle,
    ["API 不可用，当前显示 Artifact Replay 兜底数据。"]
  ),
  mapRunSummary(
    {
      run_id: "fallback-run-partial-artifacts",
      workflow_id: "source-health-runtime",
      profile: "studio-fallback",
      status: "partially_failed",
      started_at: "2026-05-22T20:30:00Z",
      finished_at: "2026-05-22T20:35:00Z",
      manifest_path: ".newsroom/runs/fallback-run-partial-artifacts/manifest.json",
      event_count: 5,
      artifact_count: 3
    },
    {
      run_id: "fallback-run-partial-artifacts",
      manifest_path: ".newsroom/runs/fallback-run-partial-artifacts/manifest.json",
      event_count: 5,
      artifact_count: 3,
      step_result_count: 1,
      integrity: { valid: false, missing_artifact_files: ["source_errors"] },
      step_results: { collect: { status: "failed" } },
      artifacts: []
    },
    ["部分 artifact 缺失，用于展示异常复盘状态。"]
  )
]

export function mapRunSummary(
  run: ApiRunListItem,
  replay?: ApiReplayBundle,
  notices: string[] = []
): StudioArtifactRunSummary {
  const runId = stringValue(run.run_id) ?? stringValue(run.id) ?? "unknown-run"
  const manifestPath = stringValue(run.manifest_path) ?? stringValue(replay?.manifest_path)
  const replayIntegrity = replay?.integrity
  const integrityValid = typeof replayIntegrity?.valid === "boolean" ? replayIntegrity.valid : undefined
  const artifactCount = numberValue(replay?.artifact_count) ?? numberValue(run.artifact_count) ?? artifactCountFromManifest(replay?.manifest)
  const eventCount = numberValue(replay?.event_count) ?? numberValue(run.event_count) ?? replay?.events?.length ?? 0
  const stepResultCount = numberValue(replay?.step_result_count) ?? countObjectKeys(replay?.step_results)
  const artifactStatus = artifactStatusFor({
    artifactCount,
    replay,
    integrityValid
  })

  return {
    runId,
    workflowId: stringValue(run.workflow_id),
    workflowVersion: stringValue(run.workflow_version),
    profile: stringValue(run.profile),
    status: stringValue(run.status) ?? "unknown",
    startedAt: stringValue(run.started_at),
    finishedAt: stringValue(run.finished_at),
    manifestPath,
    artifactCount,
    eventCount,
    stepResultCount,
    artifactStatus,
    notices
  }
}

export function mapArtifact(raw: ApiArtifact, runId: string): StudioArtifact {
  const relativePath = stringValue(raw.relative_path) ?? stringValue(raw.path)
  const artifactKey = stringValue(raw.artifact_key) ?? stringValue(raw.artifact_id) ?? relativePath ?? "artifact"
  const contentType = stringValue(raw.content_type)
  const readError = stringValue(raw.read_error)
  const previewKind = previewKindFor(contentType, relativePath ?? artifactKey)
  const sizeBytes = numberValue(raw.size_bytes)
  const preview = buildPreview(raw.content, previewKind, sizeBytes)

  return {
    artifactKey,
    runId,
    relativePath,
    contentType,
    sizeBytes,
    content: raw.content,
    readError,
    previewKind,
    redacted: containsRedactedValue(raw.content),
    truncated: preview.truncated,
    previewText: preview.text,
    previewNotice: preview.notice
  }
}

export function mapReplayBundle(
  payload: ApiReplayBundle,
  options: { fallback?: boolean; partial?: boolean; notices?: string[] } = {}
): StudioReplayBundle {
  const runId = stringValue(payload.run_id) ?? "unknown-run"
  const artifacts = (payload.artifacts ?? []).map((artifact) => mapArtifact(artifact, runId))
  const manifest = payload.manifest ?? {}
  const events = payload.events ?? []
  const stepResults = payload.step_results ?? {}
  const integrity = payload.integrity ?? {}
  const integrityValid = typeof integrity.valid === "boolean" ? integrity.valid : undefined
  const eventCount = numberValue(payload.event_count) ?? events.length
  const artifactCount = numberValue(payload.artifact_count) ?? artifacts.length
  const stepResultCount = numberValue(payload.step_result_count) ?? countObjectKeys(stepResults)
  const ready = integrityValid !== false && !payload.events_error && eventCount > 0 && artifactCount > 0
  const dataState: ArtifactDataState = options.fallback ? "fallback" : options.partial ? "partial" : "ready"

  return {
    runId,
    manifest,
    manifestPath: stringValue(payload.manifest_path),
    events,
    eventsPath: stringValue(payload.events_path),
    eventsError: stringValue(payload.events_error),
    artifacts,
    stepResults,
    integrity,
    eventCount,
    artifactCount,
    stepResultCount,
    ready,
    readinessLabel: ready ? "可复盘" : "需要检查",
    notices: options.notices ?? [],
    dataState
  }
}

export function mapLineage(
  payload: ApiLineageResult | undefined,
  direction: StudioLineageRef["direction"] = "upstream",
  fallbackRunId = "unknown-run"
): StudioLineageRef[] {
  return (payload?.lineage_refs ?? []).map((ref) => ({
    lineageId: stringValue(ref.lineage_id),
    runId: stringValue(ref.run_id) ?? stringValue(payload?.run_id) ?? fallbackRunId,
    sourceType: stringValue(ref.source_type) ?? "unknown",
    sourceId: stringValue(ref.source_id) ?? "unknown",
    targetType: stringValue(ref.target_type) ?? "unknown",
    targetId: stringValue(ref.target_id) ?? "unknown",
    relationType: stringValue(ref.relation_type),
    direction,
    createdAt: stringValue(ref.created_at)
  }))
}

export function buildRunDetail({
  run,
  artifacts,
  replay,
  lineage,
  selectedArtifactKey,
  notices = [],
  dataState = "ready"
}: {
  run: StudioArtifactRunSummary
  artifacts: StudioArtifact[]
  replay?: StudioReplayBundle
  lineage: StudioLineageRef[]
  selectedArtifactKey?: string
  notices?: string[]
  dataState?: ArtifactDataState
}): StudioArtifactRunDetail {
  const selectedArtifact =
    artifacts.find((artifact) => artifact.artifactKey === selectedArtifactKey) ??
    artifacts.find((artifact) => artifact.content !== undefined) ??
    artifacts[0]

  return {
    run,
    manifest: replay?.manifest ?? {},
    artifacts,
    selectedArtifact,
    replay,
    lineage,
    notices,
    dataState
  }
}

export function previewKindFor(contentType?: string, path?: string): ArtifactPreviewKind {
  const normalizedType = (contentType ?? "").toLowerCase()
  const normalizedPath = (path ?? "").toLowerCase()

  if (normalizedType.includes("json") || normalizedPath.endsWith(".json") || normalizedPath.endsWith(".jsonl")) {
    return "json"
  }
  if (normalizedType.includes("markdown") || normalizedPath.endsWith(".md") || normalizedPath.endsWith(".markdown")) {
    return "markdown"
  }
  if (normalizedType.includes("html") || normalizedPath.endsWith(".html") || normalizedPath.endsWith(".htm")) {
    return "html"
  }
  if (
    normalizedType.startsWith("text/") ||
    normalizedType.includes("x-ndjson") ||
    normalizedPath.endsWith(".log") ||
    normalizedPath.endsWith(".txt") ||
    normalizedPath.endsWith(".csv")
  ) {
    return "text"
  }
  if (
    normalizedType.includes("octet-stream") ||
    normalizedType.includes("pdf") ||
    normalizedType.includes("parquet") ||
    normalizedPath.endsWith(".pdf") ||
    normalizedPath.endsWith(".parquet") ||
    normalizedPath.endsWith(".png") ||
    normalizedPath.endsWith(".jpg") ||
    normalizedPath.endsWith(".jpeg") ||
    normalizedPath.endsWith(".zip")
  ) {
    return "binary"
  }
  return "unknown"
}

export function buildPreview(
  content: unknown,
  previewKind: ArtifactPreviewKind,
  sizeBytes?: number
): { text?: string; truncated: boolean; notice?: string } {
  if (previewKind === "binary") {
    return { truncated: false, notice: "二进制产物仅展示 metadata，不渲染正文。" }
  }
  if (content === undefined || content === null) {
    return { truncated: false }
  }

  const text = previewKind === "json" && typeof content !== "string" ? JSON.stringify(content, null, 2) : stringifyContent(content)
  const estimatedBytes = sizeBytes ?? byteLength(text)
  if (estimatedBytes > ARTIFACT_PREVIEW_LIMIT_BYTES || byteLength(text) > ARTIFACT_PREVIEW_LIMIT_BYTES) {
    return {
      text: truncateByChars(text, ARTIFACT_PREVIEW_LIMIT_BYTES),
      truncated: true,
      notice: "内容超过 100KB，已截断预览。"
    }
  }
  return { text, truncated: false }
}

export function mapFallbackDetail(runId: string): StudioArtifactRunDetail {
  const replayPayload = {
    ...fallbackReplayBundle,
    run_id: runId || fallbackReplayBundle.run_id
  }
  const replay = mapReplayBundle(replayPayload, {
    fallback: true,
    notices: ["API 不可用，当前显示 Artifact Replay 兜底数据。"]
  })
  const run = mapRunSummary(
    {
      run_id: replay.runId,
      workflow_id: "daily-newsroom-runtime",
      workflow_version: "fallback",
      profile: "studio-fallback",
      status: "succeeded",
      started_at: "2026-05-23T08:12:00Z",
      finished_at: "2026-05-23T08:31:00Z",
      manifest_path: replay.manifestPath,
      event_count: replay.eventCount,
      artifact_count: replay.artifactCount
    },
    replayPayload,
    replay.notices
  )
  const lineage = mapLineage(
    {
      ...fallbackLineage,
      run_id: replay.runId,
      lineage_refs: fallbackLineage.lineage_refs?.map((ref) => ({ ...ref, run_id: replay.runId }))
    },
    "upstream",
    replay.runId
  )
  return buildRunDetail({
    run,
    artifacts: replay.artifacts,
    replay,
    lineage,
    notices: replay.notices,
    dataState: "fallback"
  })
}

function artifactStatusFor({
  artifactCount,
  replay,
  integrityValid
}: {
  artifactCount: number
  replay?: ApiReplayBundle
  integrityValid?: boolean
}): StudioArtifactRunSummary["artifactStatus"] {
  if (replay?.events_error || integrityValid === false) return "error"
  if (artifactCount <= 0) return "missing"
  if (!replay) return "partial"
  return "ready"
}

function artifactCountFromManifest(manifest?: Record<string, unknown> | null): number {
  const artifacts = manifest?.artifacts
  return artifacts && typeof artifacts === "object" ? Object.keys(artifacts).length : 0
}

function countObjectKeys(value?: Record<string, unknown> | null): number {
  return value && typeof value === "object" ? Object.keys(value).length : 0
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length ? value : undefined
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function stringifyContent(content: unknown): string {
  if (typeof content === "string") return content
  return JSON.stringify(content, null, 2)
}

function containsRedactedValue(value: unknown): boolean {
  if (value === "[redacted]") return true
  if (typeof value === "string") return value.includes("[redacted]")
  if (Array.isArray(value)) return value.some(containsRedactedValue)
  if (value && typeof value === "object") {
    return Object.values(value).some(containsRedactedValue)
  }
  return false
}

function byteLength(value: string): number {
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(value).length
  return value.length
}

function truncateByChars(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit)}\n...` : value
}
