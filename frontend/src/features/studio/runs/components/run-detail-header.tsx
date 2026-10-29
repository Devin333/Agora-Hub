import { Badge } from "@/components/common/badge"
import { ScoreMeter } from "@/components/common/score-meter"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration, shortRunId } from "@/features/studio/runs/lib/run-format"
import { formatDateTime } from "@/lib/format"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { AgentRun } from "@/types/agent"

/** Run summary panel shown below the page header on run detail pages. */
export function RunDetailHeader({ run }: { run: AgentRun }) {
  const { t } = useI18n()
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <AgentRunStatusBadge status={run.status} />
            <Badge tone="accent">{run.agentName}</Badge>
            <Badge tone="neutral">{run.profile}</Badge>
          </div>
          <div>
            <h2 className="break-all text-heading3 font-semibold text-foreground">{run.workflowName ?? run.agentName}</h2>
            <p className="mt-0.5 font-mono text-label text-muted-foreground">{shortRunId(run.id)}</p>
          </div>
        </div>
        <div className="w-full max-w-xs">
          <ScoreMeter label={t("studio.quality.qualityScore")} value={run.qualityScore ?? 0} />
        </div>
      </div>

      <dl className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        <Metric label={t("studio.runs.started")} value={formatDateTime(run.startedAt)} />
        <Metric label={t("studio.runs.finished")} value={formatDateTime(run.finishedAt)} />
        <Metric label={t("studio.runs.duration")} value={formatDuration(run.durationMs)} />
        <Metric label={t("studio.runs.inputsOutputs")} value={`${run.inputCount} / ${run.outputCount}`} />
        <Metric label={t("studio.runs.stepsArtifacts")} value={`${run.stepCount ?? 0} / ${run.artifactCount}`} />
      </dl>
    </section>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-border px-3 py-2.5">
      <dt className="text-label text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 truncate text-[13.5px] font-medium tabular-nums text-foreground">{value}</dd>
    </div>
  )
}
