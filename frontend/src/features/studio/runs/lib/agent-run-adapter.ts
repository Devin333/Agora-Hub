import { fetchRunCenterDetail, fetchRunCenterList } from "@/features/studio/runs/api/run-center-api"
import { adaptRunDetail, adaptRunList } from "@/features/studio/runs/lib/run-center-adapter"
import type { AgentRun, AgentRunDetail, StudioOverview } from "@/types/agent"

export async function getStudioAgentRuns(): Promise<{ runs: AgentRun[]; notices: string[] }> {
  return adaptRunList(await fetchRunCenterList())
}

export async function getStudioAgentRunDetail(runId: string): Promise<AgentRunDetail | undefined> {
  return adaptRunDetail(runId, await fetchRunCenterDetail(runId))
}

export async function getStudioOverview(): Promise<StudioOverview> {
  const { runs } = await getStudioAgentRuns()
  const activeRuns = runs.filter((run) => run.status === "running").length
  const failedRuns24h = runs.filter((run) => run.errorCount > 0 || run.status === "failed" || run.status === "partially_failed" || run.status === "blocked" || run.status === "waiting_for_human").length
  const completedRuns24h = runs.filter((run) => run.status === "success" || run.status === "succeeded").length
  const durationValues = runs.map((run) => run.durationMs).filter((value): value is number => value !== undefined)
  const qualityValues = runs.map((run) => run.qualityScore).filter((value): value is number => value !== undefined)

  return {
    activeRuns,
    failedRuns24h,
    completedRuns24h,
    avgDurationMs: average(durationValues),
    avgQualityScore: average(qualityValues),
    artifactsGenerated24h: runs.reduce((total, run) => total + run.artifactCount, 0),
    qualityReviewRequired: runs.filter((run) => run.status === "partially_failed" || run.status === "blocked" || run.status === "waiting_for_human" || (run.qualityScore ?? 100) < 75).length,
    latestRuns: runs.slice(0, 6)
  }
}

function average(values: number[]): number | undefined {
  if (!values.length) return undefined
  return Math.round(values.reduce((total, value) => total + value, 0) / values.length)
}
