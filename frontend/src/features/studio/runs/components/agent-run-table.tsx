import Link from "next/link"
import { EmptyState } from "@/components/common/empty-state"
import { ScoreMeter } from "@/components/common/score-meter"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration, shortRunId } from "@/features/studio/runs/lib/run-format"
import { StudioTableFrame, studioTableCellClass, studioTableHeadClass } from "@/features/studio/shared/components/studio-dashboard"
import { cn } from "@/lib/utils"
import { formatDateTime } from "@/lib/format"
import type { AgentRun } from "@/types/agent"

const columns = ["运行 ID", "智能体", "配置", "状态", "开始", "耗时", "输入", "输出", "步骤", "产物", "质量", "错误"]

export function AgentRunTable({ runs }: { runs: AgentRun[] }) {
  if (!runs.length) {
    return <EmptyState title="未找到智能体运行" description="调整筛选条件，或等待下一次工作流运行。" />
  }

  return (
    <StudioTableFrame>
      <Table className="min-w-[1120px] text-body2">
        <TableHeader className="text-label">
          <TableRow className="hover:bg-transparent">
            {columns.map((column) => (
              <TableHead key={column} className={studioTableHeadClass}>
                {column}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => (
            <TableRow key={run.id}>
              <TableCell className={studioTableCellClass}>
                <Link className="font-mono text-label font-medium text-link hover:underline" href={`/studio/runs/${encodeURIComponent(run.id)}`}>
                  {shortRunId(run.id)}
                </Link>
              </TableCell>
              <TableCell className={cn(studioTableCellClass, "font-medium text-foreground")}>{run.agentName}</TableCell>
              <TableCell className={cn(studioTableCellClass, "text-muted-foreground")}>{run.profile}</TableCell>
              <TableCell className={studioTableCellClass}><AgentRunStatusBadge status={run.status} /></TableCell>
              <TableCell className={cn(studioTableCellClass, "text-muted-foreground")}>{formatDateTime(run.startedAt)}</TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{formatDuration(run.durationMs)}</TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{run.inputCount}</TableCell>
              <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{run.outputCount}</TableCell>
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
