import {
  fallbackDetailForRun,
  studioAgentRunDetails,
  studioAgentRuns
} from "@/features/studio/runs/lib/mock-agent-runs"
import type {
  ApiRunArtifact,
  ApiRunDetail,
  ApiRunEvents,
  ApiRunList,
  ApiRunListItem,
  ApiRunStep,
  ApiRunSteps,
  RunCenterDetailResponses
} from "@/features/studio/runs/api/run-center-api"
import type { SafeApiResult } from "@/lib/api/server"
import type {
  AgentRun,
  AgentRunStatus,
  AgentStep,
  Artifact,
  RunErrorTrace,
  RunLogItem,
  RunQualitySummary,
  StepStatus,
  StudioOverview,
  StudioDataState,
  StudioRunDetail,
  StudioRunListItem,
  StudioRunOperations,
  WorkflowDagEdge,
  WorkflowDagNode
} from "@/types/agent"

type ApiRunDiagnostics = {
  diagnostics?: Record<string, unknown> | null
}

export function adaptRunList(response: SafeApiResult<ApiRunList>): { runs: StudioRunListItem[]; notices: string[] } {
  if (!response.ok || !response.data.runs?.length) {
    return {
      runs: studioAgentRuns.map((run) => toStudioRun(run, { dataState: "fallback", notices: ["当前为 fallback 数据"] })),
      notices: [response.ok ? "当前为 fallback 数据：API 未返回运行记录。" : `当前为 fallback 数据：${response.errorMessage}`]
    }
  }

  const apiRuns = response.data.runs
    .map((item) => mapApiRun(item, mockRunFor(runId(item))))
    .filter((run): run is StudioRunListItem => Boolean(run))

  return {
    runs: apiRuns,
    notices: []
  }
}

export function buildStudioOverview(runs: StudioRunListItem[]): StudioOverview {
  const sortedRuns = [...runs].sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime())
  const latestWindowStart = latestRunWindowStart(sortedRuns)
  const recentRuns = sortedRuns.filter((run) => new Date(run.startedAt).getTime() >= latestWindowStart)
  const completedRuns = recentRuns.filter((run) => run.status === "success" || run.status === "succeeded")
  const qualityScores = runs.map((run) => run.qualityScore).filter((value): value is number => typeof value === "number")
  const durations = runs.map((run) => run.durationMs).filter((value): value is number => typeof value === "number" && value > 0)

  return {
    activeRuns: runs.filter((run) => run.status === "running" || run.status === "pending" || run.status === "waiting_for_human" || run.status === "blocked").length,
    failedRuns24h: recentRuns.filter((run) => run.status === "failed" || run.status === "partially_failed").length,
    completedRuns24h: completedRuns.length,
    avgDurationMs: average(durations),
    avgQualityScore: average(qualityScores),
    artifactsGenerated24h: recentRuns.reduce((total, run) => total + run.artifactCount, 0),
    qualityReviewRequired: runs.filter((run) => (run.qualityScore ?? 100) < 75 || run.errorCount > 0 || run.status === "partially_failed").length,
    latestRuns: sortedRuns.slice(0, 5)
  }
}

export function adaptRunDetail(runIdValue: string, responses: RunCenterDetailResponses): StudioRunDetail | undefined {
  const decodedRunId = decodeURIComponent(runIdValue)
  const mockRun = mockRunFor(decodedRunId)
  const apiRun = responses.detail.ok ? mapApiRun(responses.detail.data, mockRun) : undefined
  const baseRun = apiRun ?? (mockRun ? toStudioRun(mockRun, { dataState: "fallback", notices: ["当前为 fallback 数据"] }) : undefined)

  if (!baseRun) return undefined

  const fallback = studioAgentRunDetails[baseRun.id] ?? fallbackDetailForRun(baseRun)
  const okCount = resultOkCount([responses.detail, responses.steps, responses.events, responses.diagnostics, responses.health])
  const dataState = detailDataState(responses, okCount)
  const notices = detailNotices(dataState, responses)
  const run = { ...baseRun, dataState, notices }
  const manifest = responses.detail.ok ? responses.detail.data.manifest : undefined
  const steps = buildSteps(baseRun.id, responses.steps.ok ? responses.steps.data : undefined, manifest, fallback.steps)
  const dag = buildDag(steps, fallback.dag.edges)
  const logs = responses.events.ok ? mapApiEvents(responses.events.data, baseRun.id) : fallback.logs
  const artifacts = responses.artifacts.ok ? mapApiArtifacts(responses.artifacts.data.artifacts ?? [], baseRun.id) : fallback.artifacts
  const diagnostics = responses.diagnostics.ok ? responses.diagnostics.data.diagnostics ?? {} : undefined
  const health = responses.health.ok ? responses.health.data.health ?? {} : undefined
  const errors = mapErrors(run, responses.detail, responses.diagnostics, fallback.errors)
  const quality = mapQuality(run, responses.detail, responses.diagnostics) ?? fallback.quality

  return {
    ...fallback,
    run,
    steps,
    dag,
    logs,
    events: logs,
    artifacts,
    quality,
    errors,
    diagnostics,
    health,
    operations: operationCapabilities(run, dataState, defaultSelectedStep(steps)),
    dataState,
    notices
  }
}

export function operationCapabilities(
  run: Pick<StudioRunListItem, "status">,
  dataState: StudioDataState,
  selectedStep?: Pick<AgentStep, "status">
): StudioRunOperations {
  if (dataState === "fallback") {
    return { canCancel: false, canRerunFromStep: false, canSkipStep: false, canResolveBlocked: false }
  }

  const canCancel = ["pending", "running", "blocked", "waiting_for_human"].includes(run.status)
  const canRerunFromStep = Boolean(selectedStep)
  const canSkipStep = Boolean(selectedStep && ["pending", "running", "failed"].includes(selectedStep.status))
  const canResolveBlocked = run.status === "blocked" || run.status === "waiting_for_human"
  return { canCancel, canRerunFromStep, canSkipStep, canResolveBlocked }
}

export function normalizeRunStatus(status: string | undefined | null): AgentRunStatus {
  if (status === "queued") return "pending"
  if (status === "completed" || status === "complete" || status === "succeeded") return "success"
  if (status === "review_required" || status === "human_review_required" || status === "waiting_for_approval") return "waiting_for_human"
  if (
    status === "pending" ||
    status === "running" ||
    status === "success" ||
    status === "failed" ||
    status === "cancelled" ||
    status === "partially_failed" ||
    status === "blocked" ||
    status === "waiting_for_human"
  ) {
    return status
  }
  return "pending"
}

export function normalizeStepStatus(status: string | undefined | null): StepStatus {
  if (status === "succeeded" || status === "completed" || status === "complete") return "success"
  if (status === "paused" || status === "waiting_for_human" || status === "review_required") return "blocked"
  if (status === "pending" || status === "running" || status === "success" || status === "failed" || status === "skipped" || status === "blocked" || status === "cancelled") {
    return status
  }
  return "pending"
}

function mapApiRun(item: ApiRunListItem | ApiRunDetail | undefined, fallback?: AgentRun): StudioRunListItem | undefined {
  if (!item) return fallback ? toStudioRun(fallback, { dataState: "fallback", notices: ["当前为 fallback 数据"] }) : undefined

  const id = runId(item)
  if (!id) return undefined

  const manifest = "manifest" in item ? item.manifest : undefined
  const startedAt = item.started_at ?? readString(recordValue(manifest, "started_at")) ?? fallback?.startedAt ?? new Date(0).toISOString()
  const finishedAt = item.finished_at ?? readString(recordValue(manifest, "finished_at")) ?? fallback?.finishedAt
  const durationMs = durationBetween(startedAt, finishedAt) ?? fallback?.durationMs
  const workflowId = item.workflow_id ?? readString(recordValue(manifest, "workflow_id")) ?? fallback?.workflowId ?? fallback?.workflowName
  const workflowName = item.workflow_name ?? workflowId ?? fallback?.workflowName ?? "agent_workflow"
  const status = normalizeRunStatus(item.status ?? readString(recordValue(manifest, "status")) ?? fallback?.status)
  const metrics = "metrics" in item ? item.metrics : undefined
  const stepCount = numberValue(item.step_count ?? recordValue(manifest, "step_count") ?? recordValue(metrics, "step_count") ?? fallback?.stepCount)
  const eventCount = numberValue(item.event_count ?? recordValue(manifest, "event_count") ?? recordValue(metrics, "event_count") ?? fallback?.eventCount)

  return {
    id,
    agentName: fallback?.agentName ?? labelFromWorkflow(workflowName),
    workflowId,
    workflowName,
    workflowVersion: item.workflow_version ?? readString(recordValue(manifest, "workflow_version")) ?? fallback?.workflowVersion,
    profile: item.profile ?? readString(recordValue(manifest, "profile")) ?? fallback?.profile ?? "default",
    status,
    startedAt,
    finishedAt,
    durationMs,
    durationSeconds: Math.round((durationMs ?? 0) / 1000),
    inputCount: fallback?.inputCount ?? eventCount ?? 0,
    outputCount: fallback?.outputCount ?? 0,
    artifactCount: numberValue(item.artifact_count ?? recordValue(manifest, "artifact_count") ?? fallback?.artifactCount) ?? 0,
    artifactDir: item.artifact_dir ?? fallback?.artifactDir,
    qualityScore: normalizeQualityScore(item.quality_score ?? fallback?.qualityScore),
    errorCount: numberValue(item.error_count ?? fallback?.errorCount) ?? (status === "failed" || status === "blocked" || status === "waiting_for_human" ? 1 : 0),
    eventCount,
    reportId: item.report_id ?? fallback?.reportId,
    manifestPath: item.manifest_path ?? fallback?.manifestPath,
    stepCount,
    steps: fallback?.steps,
    dataState: "ready",
    notices: []
  }
}

function toStudioRun(run: AgentRun, options: { dataState: StudioDataState; notices: string[] }): StudioRunListItem {
  return {
    ...run,
    workflowId: run.workflowId ?? run.workflowName,
    workflowVersion: run.workflowVersion,
    reportId: run.reportId,
    artifactDir: run.artifactDir,
    eventCount: run.eventCount,
    manifestPath: run.manifestPath,
    dataState: options.dataState,
    notices: options.notices
  }
}

function buildSteps(
  runIdValue: string,
  apiSteps: ApiRunSteps | undefined,
  manifest: Record<string, unknown> | null | undefined,
  fallbackSteps: AgentStep[]
): AgentStep[] {
  if (apiSteps?.steps?.length) {
    return apiSteps.steps.map((step, index) => mapApiStep(runIdValue, step, index))
  }

  const manifestSteps = manifest?.steps
  if (manifestSteps && typeof manifestSteps === "object") {
    const path = Array.isArray(manifest?.path) ? manifest.path.map(String) : []
    return Object.entries(manifestSteps as Record<string, unknown>)
      .map(([stepId, raw], index) => mapManifestStep(runIdValue, stepId, raw, path.indexOf(stepId), index))
      .sort((a, b) => stepOrder(a, path) - stepOrder(b, path))
  }

  return fallbackSteps
}

function mapApiStep(runIdValue: string, step: ApiRunStep, index: number): AgentStep {
  const id = step.step_id ?? step.id ?? `step-${index + 1}`
  const raw = step.raw
  const rawOutputs = recordValue(raw, "outputs")
  const rawInputs = recordValue(raw, "inputs")
  const artifactRefs = arrayValue(step.artifact_refs ?? recordValue(raw, "artifact_refs"))

  return {
    id,
    runId: runIdValue,
    nodeId: `node-${id}`,
    label: step.label ?? humanLabel(id),
    type: stepType(step.type ?? id),
    status: normalizeStepStatus(step.status),
    startedAt: step.started_at ?? readString(recordValue(raw, "started_at")),
    finishedAt: step.finished_at ?? readString(recordValue(raw, "finished_at")),
    durationMs: durationBetween(step.started_at, step.finished_at),
    inputPreview: rawInputs,
    outputPreview: rawOutputs ?? { outputKeys: step.output_keys ?? [] },
    errorMessage: errorMessage(step.error ?? recordValue(raw, "error")),
    artifactIds: artifactRefs.map((item) => (typeof item === "string" ? item : readString(recordValue(item, "artifact_key")) ?? readString(recordValue(item, "id")))).filter(Boolean) as string[],
    raw: step.raw ?? step
  }
}

function mapManifestStep(runIdValue: string, stepId: string, raw: unknown, sequence: number, index: number): AgentStep {
  const record = isRecord(raw) ? raw : { value: raw }
  return {
    id: stepId,
    runId: runIdValue,
    nodeId: `node-${stepId}`,
    label: humanLabel(stepId),
    type: stepType(stepId),
    status: normalizeStepStatus(readString(record.status)),
    startedAt: readString(record.started_at),
    finishedAt: readString(record.finished_at),
    durationMs: durationBetween(readString(record.started_at), readString(record.finished_at)),
    inputPreview: record.inputs,
    outputPreview: record.outputs ?? { sequence: sequence >= 0 ? sequence : index },
    errorMessage: errorMessage(record.error),
    artifactIds: arrayValue(record.artifact_refs).map(String),
    raw: record
  }
}

function buildDag(steps: AgentStep[], fallbackEdges: WorkflowDagEdge[]): { nodes: WorkflowDagNode[]; edges: WorkflowDagEdge[] } {
  const nodes = steps.map((step) => ({
    id: step.nodeId,
    stepId: step.id,
    label: step.label,
    type: step.type,
    status: step.status,
    durationMs: step.durationMs,
    inputCount: step.inputPreview ? 1 : 0,
    outputCount: step.outputPreview ? 1 : 0,
    errorMessage: step.errorMessage
  }))

  const nodeIds = new Set(nodes.map((node) => node.id))
  const reusableEdges = fallbackEdges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
  if (reusableEdges.length) return { nodes, edges: reusableEdges }

  return {
    nodes,
    edges: steps.slice(1).map((step, index) => ({
      id: `${steps[index].nodeId}-${step.nodeId}`,
      source: steps[index].nodeId,
      target: step.nodeId
    }))
  }
}

function mapApiEvents(events: ApiRunEvents, runIdValue: string): RunLogItem[] {
  return (events.events ?? []).map((event, index) => {
    const eventType = event.event_type ?? "run_event"
    const message = event.message ?? readString(recordValue(event.payload, "message")) ?? eventType
    return {
      id: event.event_id ?? event.id ?? `${runIdValue}-event-${index + 1}`,
      timestamp: event.occurred_at ?? event.timestamp ?? event.created_at ?? new Date(0).toISOString(),
      level: normalizeLogLevel(event.level ?? eventType),
      message,
      stepId: event.step_id ?? readString(recordValue(event.payload, "step_id")),
      eventType,
      payload: event.payload ?? undefined
    }
  })
}

function mapApiArtifacts(items: ApiRunArtifact[], runIdValue: string): Artifact[] {
  return items.map((item, index) => {
    const id = item.artifact_key ?? item.relative_path ?? `api-artifact-${index + 1}`
    const filename = item.relative_path?.split(/[\\/]/).pop() ?? id
    return {
      id,
      runId: runIdValue,
      artifactType: artifactTypeFromContent(item.content_type ?? "", filename),
      filename,
      sizeBytes: item.size_bytes ?? undefined,
      createdAt: new Date(0).toISOString(),
      preview: item.read_error ? `Read error: ${item.read_error}` : previewFromContent(item.content),
      url: item.url ?? undefined
    }
  })
}

function mapErrors(
  run: StudioRunListItem,
  detailResult: SafeApiResult<ApiRunDetail>,
  diagnosticsResult: SafeApiResult<ApiRunDiagnostics>,
  fallbackErrors: RunErrorTrace[]
): RunErrorTrace[] {
  const detailError = detailResult.ok ? detailResult.data.error : undefined
  const diagnostics = diagnosticsResult.ok ? diagnosticsResult.data.diagnostics : undefined
  const diagnosticsError = recordValue(diagnostics, "error") ?? recordValue(diagnostics, "last_error")
  const message = errorMessage(detailError) ?? errorMessage(diagnosticsError)
  if (!message) return fallbackErrors

  return [
    {
      id: `${run.id}-api-error`,
      runId: run.id,
      timestamp: run.finishedAt ?? run.startedAt,
      message,
      stackPreview: isRecord(detailError) ? readString(detailError.stack) : undefined,
      retryHint: "Inspect diagnostics and rerun from the failed step after the root cause is resolved."
    }
  ]
}

function mapQuality(
  run: StudioRunListItem,
  detailResult: SafeApiResult<ApiRunDetail>,
  diagnosticsResult: SafeApiResult<ApiRunDiagnostics>
): RunQualitySummary | undefined {
  const outputPreview = detailResult.ok ? detailResult.data.output_preview : undefined
  const diagnostics = diagnosticsResult.ok ? diagnosticsResult.data.diagnostics : undefined
  const qualityTrace = recordValue(outputPreview, "quality_trace") ?? recordValue(diagnostics, "quality_trace") ?? recordValue(diagnostics, "health_report")
  if (!qualityTrace && run.qualityScore === undefined) return undefined

  const warning = run.status === "blocked" || run.status === "waiting_for_human" || run.errorCount > 0
  return {
    runId: run.id,
    score: run.qualityScore,
    status: warning ? "review_required" : "passed",
    checks: [
      {
        id: "api-quality-summary",
        name: "API quality trace",
        status: warning ? "warning" : "passed",
        message: qualityTrace ? JSON.stringify(qualityTrace).slice(0, 240) : "Quality score loaded from run metadata."
      }
    ]
  }
}

function detailDataState(responses: RunCenterDetailResponses, okCount: number): StudioDataState {
  if (okCount === 0) return "fallback"
  const hasApiSteps = responses.steps.ok && Boolean(responses.steps.data.steps?.length)
  return responses.detail.ok && hasApiSteps && responses.events.ok && responses.diagnostics.ok && responses.health.ok ? "ready" : "partial"
}

function detailNotices(dataState: StudioDataState, responses: RunCenterDetailResponses): string[] {
  if (dataState === "ready") return []
  if (dataState === "fallback") return ["当前为 fallback 数据：Run Runtime API 不可用。"]

  const failed = [
    ["detail", responses.detail],
    ["steps", responses.steps],
    ["events", responses.events],
    ["diagnostics", responses.diagnostics],
    ["health", responses.health],
    ["artifacts", responses.artifacts]
  ]
    .filter(([, result]) => !(result as SafeApiResult<unknown>).ok)
    .map(([name]) => name)

  return [`API 响应不完整，当前为 partial 数据。缺失：${failed.join(", ")}`]
}

function defaultSelectedStep(steps: AgentStep[]): AgentStep | undefined {
  return steps.find((step) => step.status === "failed" || step.status === "blocked") ?? steps.find((step) => step.status === "running") ?? steps[0]
}

function runId(item?: ApiRunListItem | ApiRunDetail): string | undefined {
  return item?.run_id ?? item?.id ?? undefined
}

function mockRunFor(id?: string): AgentRun | undefined {
  return id ? studioAgentRuns.find((run) => run.id === id) : undefined
}

function resultOkCount(results: SafeApiResult<unknown>[]): number {
  return results.filter((result) => result.ok).length
}

function latestRunWindowStart(runs: StudioRunListItem[]): number {
  const latest = runs[0] ? new Date(runs[0].startedAt).getTime() : Date.now()
  if (Number.isNaN(latest)) return Date.now() - 24 * 60 * 60 * 1000
  return latest - 24 * 60 * 60 * 1000
}

function average(values: number[]): number | undefined {
  if (!values.length) return undefined
  return Math.round(values.reduce((total, value) => total + value, 0) / values.length)
}

function normalizeQualityScore(value: number | undefined | null): number | undefined {
  if (value === undefined || value === null) return undefined
  return value <= 1 ? Math.round(value * 100) : Math.round(value)
}

function durationBetween(startedAt?: string | null, finishedAt?: string | null): number | undefined {
  if (!startedAt || !finishedAt) return undefined
  const started = new Date(startedAt).getTime()
  const finished = new Date(finishedAt).getTime()
  if (Number.isNaN(started) || Number.isNaN(finished)) return undefined
  return Math.max(0, finished - started)
}

function labelFromWorkflow(value: string): string {
  return value
    .split(/[_-]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ")
}

function humanLabel(value: string): string {
  return labelFromWorkflow(value.replace(/^step-/, ""))
}

function stepType(value: string | null | undefined): AgentStep["type"] {
  const text = value ?? ""
  if (text.includes("collect") || text.includes("source")) return "collect"
  if (text.includes("process") || text.includes("normalize") || text.includes("dedupe")) return "process"
  if (text.includes("rank") || text.includes("score")) return "rank"
  if (text.includes("memory")) return "memory"
  if (text.includes("research") || text.includes("evidence")) return "research"
  if (text.includes("report") || text.includes("write")) return "report"
  if (text.includes("quality") || text.includes("verify")) return "quality"
  if (text.includes("final") || text.includes("publish")) return "final"
  return "custom"
}

function normalizeLogLevel(value: string): RunLogItem["level"] {
  if (value.includes("failed") || value.includes("error")) return "error"
  if (value.includes("warning") || value.includes("warn") || value.includes("blocked")) return "warning"
  if (value.includes("debug")) return "debug"
  return "info"
}

function artifactTypeFromContent(contentType: string, filename: string): Artifact["artifactType"] {
  if (contentType.includes("json") || filename.endsWith(".json")) return "json"
  if (contentType.includes("html") || filename.endsWith(".html")) return "html"
  if (contentType.includes("markdown") || filename.endsWith(".md")) return "markdown"
  if (contentType.includes("text") || filename.endsWith(".log")) return "log"
  if (filename.includes("report")) return "report"
  return "dataset"
}

function previewFromContent(content: unknown): string | undefined {
  if (content === undefined || content === null) return undefined
  return typeof content === "string" ? content : JSON.stringify(content, null, 2)
}

function errorMessage(value: unknown): string | undefined {
  if (!value) return undefined
  if (typeof value === "string") return value
  if (isRecord(value)) return readString(value.message) ?? readString(value.error) ?? readString(value.detail)
  return undefined
}

function stepOrder(step: AgentStep, path: string[]): number {
  const index = path.indexOf(step.id)
  return index >= 0 ? index : Number.MAX_SAFE_INTEGER
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : undefined
  }
  return undefined
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" && value.length ? value : undefined
}

function recordValue(value: unknown, key: string): unknown {
  return isRecord(value) ? value[key] : undefined
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value))
}
