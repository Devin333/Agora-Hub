import Link from "next/link"
import { EmptyState } from "@/components/common/empty-state"
import { ScoreMeter } from "@/components/common/score-meter"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration, shortRunId } from "@/features/studio/runs/lib/run-format"
import { StudioTableFrame } from "@/features/studio/shared/components/studio-dashboard"
import { formatDataState } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import { cn } from "@/lib/utils"
import { formatDateTime } from "@/lib/format"
import type { StudioRunListItem } from "@/types/agent"

export function RunListTable({ runs }: { runs: StudioRunListItem[] }) {
  const { locale, t } = useI18n()
  if (!runs.length) {
    return <EmptyState title={t("studio.runs.noRuns")} description={t("studio.runs.noRunsDescription")} />
  }

  return (
    <StudioTableFrame>
      <div className="overflow-x-auto">
      <table className="w-full min-w-[1340px] border-collapse text-left text-sm">
        <thead className="border-b border-border bg-secondary/80 text-xs uppercase text-muted-foreground">
          <tr>
            <th className="px-4 py-3 font-medium">{t("studio.runs.runId")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.agent")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.workflow")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.profile")}</th>
            <th className="px-4 py-3 font-medium">{t("common.status")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.started")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.duration")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.inputsOutputs")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.steps")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.quality.artifacts")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.boards.quality")}</th>
            <th className="px-4 py-3 font-medium">{t("studio.runs.errors")}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.id}
              className={cn(
                "border-b border-border/70 last:border-b-0 hover:bg-secondary/40",
                run.status === "failed" && "bg-destructive/5",
                (run.status === "blocked" || run.status === "waiting_for_human") && "bg-warning/5"
              )}
            >
              <td className="px-4 py-3">
                <Link className="font-mono text-xs font-medium text-link hover:underline" href={`/studio/runs/${encodeURIComponent(run.id)}`}>
                  {shortRunId(run.id)}
                </Link>
                {run.dataState === "fallback" ? <p className="mt-1 text-xs text-warning">{formatDataState(locale, "fallback")}</p> : null}
              </td>
              <td className="px-4 py-3 text-muted-foreground">{run.agentName}</td>
              <td className="px-4 py-3">
                <p className="font-medium text-foreground">{run.workflowName ?? run.workflowId ?? run.agentName}</p>
                {run.workflowVersion ? <p className="mt-1 text-xs text-muted-foreground">{run.workflowVersion}</p> : null}
              </td>
              <td className="px-4 py-3 text-muted-foreground">{run.profile}</td>
              <td className="px-4 py-3"><AgentRunStatusBadge status={run.status} /></td>
              <td className="px-4 py-3 text-muted-foreground">{formatDateTime(run.startedAt)}</td>
              <td className="px-4 py-3 text-muted-foreground">{formatDuration(run.durationMs)}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.inputCount} / {run.outputCount}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.stepCount ?? 0}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.artifactCount}</td>
              <td className="w-36 px-4 py-3"><ScoreMeter value={run.qualityScore ?? 0} /></td>
              <td className={run.errorCount > 0 ? "px-4 py-3 font-semibold text-destructive" : "px-4 py-3 text-muted-foreground"}>{run.errorCount}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </StudioTableFrame>
  )
}
