import { Activity, AlertTriangle, CheckCircle2, Clock, PauseCircle } from "lucide-react"
import { formatDuration } from "@/features/studio/runs/lib/run-format"
import { StudioMetricCard, StudioMetricGrid } from "@/features/studio/shared/components/studio-dashboard"
import type { StudioRunListItem } from "@/types/agent"

export function RunStatusSummary({ runs }: { runs: StudioRunListItem[] }) {
  const running = runs.filter((run) => run.status === "running").length
  const failed = runs.filter((run) => run.status === "failed").length
  const waiting = runs.filter((run) => run.status === "blocked" || run.status === "waiting_for_human").length
  const completed = runs.filter((run) => run.status === "success" || run.status === "succeeded").length
  const durations = runs.map((run) => run.durationMs).filter((value): value is number => value !== undefined)
  const avgDuration = durations.length ? Math.round(durations.reduce((total, value) => total + value, 0) / durations.length) : undefined

  return (
    <StudioMetricGrid className="xl:grid-cols-5 2xl:grid-cols-5">
      <StudioMetricCard label="Total runs" value={runs.length} detail="Loaded from /api/v1/runs" icon={Activity} />
      <StudioMetricCard label="Running" value={running} detail="Active workflows" icon={Clock} tone="info" />
      <StudioMetricCard label="Completed" value={completed} detail="Successful runs" icon={CheckCircle2} tone="success" />
      <StudioMetricCard label="Needs attention" value={failed + waiting} detail={`${failed} failed, ${waiting} blocked`} icon={AlertTriangle} tone={failed + waiting ? "warning" : "neutral"} />
      <StudioMetricCard label="Average duration" value={formatDuration(avgDuration)} detail="Across loaded runs" icon={PauseCircle} />
    </StudioMetricGrid>
  )
}
