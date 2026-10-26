import { Badge } from "@/components/common/badge"
import { JsonPreview } from "@/features/studio/runs/components/json-preview"
import { formatDuration, formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import { formatDateTime } from "@/lib/format"
import type { ToolCall } from "@/types/agent"

export function ToolCallCard({ call }: { call: ToolCall }) {
  return (
    <article className="space-y-3 rounded-md border border-border bg-secondary/35 p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">{call.toolName}</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            {formatDateTime(call.startedAt)} · {formatDuration(call.durationMs)}
          </p>
        </div>
        <Badge tone={statusTone(call.status)}>{formatRunStatus(call.status)}</Badge>
      </div>
      {call.errorMessage ? <p className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-sm text-destructive">{call.errorMessage}</p> : null}
      <JsonPreview label="参数" value={call.argsPreview} />
      <JsonPreview label="结果" value={call.resultPreview} />
    </article>
  )
}
