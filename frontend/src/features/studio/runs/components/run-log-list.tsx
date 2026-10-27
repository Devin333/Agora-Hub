import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { formatDateTime } from "@/lib/format"
import type { RunLogItem } from "@/types/agent"

const levelTone = {
  debug: "neutral",
  info: "info",
  warning: "warning",
  error: "danger"
} as const

export function RunLogList({ logs }: { logs: RunLogItem[] }) {
  if (!logs.length) return <EmptyState title="暂无日志" description="这次运行没有日志条目。" />

  return (
    <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
      {logs.map((log) => (
        <div key={log.id} className="px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Badge tone={levelTone[log.level]}>{log.level}</Badge>
            <span className="text-label text-muted-foreground">{formatDateTime(log.timestamp)}</span>
          </div>
          <p className="mt-1.5 text-[13.5px] text-foreground">{log.message}</p>
          {log.stepId ? <p className="mt-0.5 font-mono text-label text-muted-foreground">{log.stepId}</p> : null}
        </div>
      ))}
    </div>
  )
}
