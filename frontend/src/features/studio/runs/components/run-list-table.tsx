import Link from "next/link"
import { EmptyState } from "@/components/common/empty-state"
import { ScoreMeter } from "@/components/common/score-meter"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration, shortRunId } from "@/features/studio/runs/lib/run-format"
import { StudioTableFrame, studioTableCellClass, studioTableHeadClass } from "@/features/studio/shared/components/studio-dashboard"
import { formatDataState } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import { cn } from "@/lib/utils"
import { formatDateTime } from "@/lib/format"
import type { StudioRunListItem } from "@/types/agent"

const columnKeys = [
  "studio.runs.runId",
  "studio.runs.agent",
  "studio.runs.workflow",
  "studio.runs.profile",
  "common.status",
  "studio.runs.started",
  "studio.runs.duration",
  "studio.runs.inputsOutputs",
  "studio.runs.steps",
  "studio.quality.artifacts",
  "studio.boards.quality",
  "studio.runs.errors"
]

export function RunListTable({ runs }: { runs: StudioRunListItem[] }) {
  const { locale, t } = useI18n()
  if (!runs.length) {
    return <EmptyState title={t("studio.runs.noRuns")} description={t("studio.runs.noRunsDescription")} />
  }

  return (
    <StudioTableFrame>
      <Table className="min-w-[1340px] text-body2">
        <TableHeader className="text-label">
          <TableRow className="hover:bg-transparent">
            {columnKeys.map((key) => (
              <TableHead key={key} className={studioTableHeadClass}>
                {t(key)}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => (
            <TableRow
              key={run.id}
              className={cn(
                "hover:bg-accent/40",
                run.status === "failed" && "bg-destructive/5",
                (run.status === "blocked" || run.status === "waiting_for_human") && "bg-warning/5"
              )}
            >
              <TableCell className={studioTableCellClass}>
                <Link className="font-mono text-label font-medium text-link hover:underline" href={`/studio/runs/${encodeURIComponent(run.id)}`}>
                  {shortRunId(run.id)}
                </Link>
                {run.dataState === "fallback" ? <p className="mt-0.5 text-label text-warning-text">{formatDataState(locale, "fallback")}</p> : null}
              </TableCell>
              <TableCell className={cn(studioTableCellClass, "text-muted-foreground")}>{run.agentName}</TableCell>
              <TableCell className={studioTableCellClass}>
                <p className="text-[13.5px] font-medium text-foreground">{run.workflowName ?? run.workflowId ?? run.agentName}</p>
                {run.workflowVersion ? <p className="mt-0.5 text-label text-muted-foreground">{run.workflowVersion}</p> : null}
              </TableCell>
              <TableCell className={cn(studioTableCellClass, "text-muted-foreground")}>{run.profile}</TableCell>
              <TableCell className={studioTableCellClass}><AgentRunStatusBadge status={run.status} /></TableCell>
              <TableCell className={cn(studioTableCellClass, "text-muted-foreground")}>{formatDateTime(run.startedAt)}</TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{formatDuration(run.durationMs)}</TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{run.inputCount} / {run.outputCount}</TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{run.stepCount ?? 0}</TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{run.artifactCount}</TableCell>
              <TableCell className={cn(studioTableCellClass, "w-36")}><ScoreMeter value={run.qualityScore ?? 0} /></TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums", run.errorCount > 0 ? "font-semibold text-destructive-text" : "text-muted-foreground")}>{run.errorCount}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </StudioTableFrame>
  )
}
