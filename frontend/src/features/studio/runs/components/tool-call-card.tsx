import { Badge } from "@/components/common/badge"
import { JsonPreview } from "@/features/studio/runs/components/json-preview"
import { formatDuration, formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import { formatDateTime } from "@/lib/format"
import type { ToolCall } from "@/types/agent"

export function ToolCallCard({ call }: { call: ToolCall }) {
  return (
    <article className="space-y-3 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[13.5px] font-medium text-foreground">{call.toolName}</h3>
          <p className="mt-0.5 text-label text-muted-foreground">
            {formatDateTime(call.startedAt)} · {formatDuration(call.durationMs)}
          </p>
        </div>
        <Badge tone={statusTone(call.status)}>{formatRunStatus(call.status)}</Badge>
      </div>
      {call.errorMessage ? <p className="rounded-lg bg-destructive/10 px-3 py-2 text-body2 text-destructive-text">{call.errorMessage}</p> : null}
      <JsonPreview label="参数" value={call.argsPreview} />
      <JsonPreview label="结果" value={call.resultPreview} />
    </article>
  )
}
