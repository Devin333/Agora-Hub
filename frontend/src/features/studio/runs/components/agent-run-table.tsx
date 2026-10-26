import Link from "next/link"
import { EmptyState } from "@/components/common/empty-state"
import { ScoreMeter } from "@/components/common/score-meter"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration, shortRunId } from "@/features/studio/runs/lib/run-format"
import { formatDateTime } from "@/lib/format"
import type { AgentRun } from "@/types/agent"

export function AgentRunTable({ runs }: { runs: AgentRun[] }) {
  if (!runs.length) {
    return <EmptyState title="未找到智能体运行" description="调整筛选条件，或等待下一次工作流运行。" />
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card">
      <table className="w-full min-w-[1120px] border-collapse text-left text-sm">
        <thead className="border-b border-border bg-secondary/70 text-xs uppercase text-muted-foreground">
          <tr>
            <th className="px-4 py-3 font-medium">运行 ID</th>
            <th className="px-4 py-3 font-medium">智能体</th>
            <th className="px-4 py-3 font-medium">配置</th>
            <th className="px-4 py-3 font-medium">状态</th>
            <th className="px-4 py-3 font-medium">开始</th>
            <th className="px-4 py-3 font-medium">耗时</th>
            <th className="px-4 py-3 font-medium">输入</th>
            <th className="px-4 py-3 font-medium">输出</th>
            <th className="px-4 py-3 font-medium">步骤</th>
            <th className="px-4 py-3 font-medium">产物</th>
            <th className="px-4 py-3 font-medium">质量</th>
            <th className="px-4 py-3 font-medium">错误</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-b border-border/70 last:border-b-0 hover:bg-secondary/40">
              <td className="px-4 py-3">
                <Link className="font-mono text-xs font-medium text-link hover:underline" href={`/studio/runs/${encodeURIComponent(run.id)}`}>
                  {shortRunId(run.id)}
                </Link>
              </td>
              <td className="px-4 py-3 font-medium text-foreground">{run.agentName}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.profile}</td>
              <td className="px-4 py-3"><AgentRunStatusBadge status={run.status} /></td>
              <td className="px-4 py-3 text-muted-foreground">{formatDateTime(run.startedAt)}</td>
              <td className="px-4 py-3 text-muted-foreground">{formatDuration(run.durationMs)}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.inputCount}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.outputCount}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.stepCount ?? 0}</td>
              <td className="px-4 py-3 text-muted-foreground">{run.artifactCount}</td>
              <td className="w-36 px-4 py-3"><ScoreMeter value={run.qualityScore ?? 0} /></td>
              <td className={run.errorCount > 0 ? "px-4 py-3 font-semibold text-destructive" : "px-4 py-3 text-muted-foreground"}>{run.errorCount}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
