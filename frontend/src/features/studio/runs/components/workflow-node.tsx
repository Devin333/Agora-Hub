import { AlertTriangle } from "lucide-react"
import type { NodeProps } from "reactflow"
import { Badge } from "@/components/common/badge"
import { formatDuration, formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import type { WorkflowDagNode } from "@/types/agent"

export function WorkflowNode({ data }: NodeProps<WorkflowDagNode>) {
  return (
    <div className="min-w-48 rounded-md bg-card p-3 shadow-sm ring-1 ring-foreground/10">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-foreground">{data.label}</p>
          <p className="mt-1 text-xs text-muted-foreground">{data.type}</p>
        </div>
        {data.status === "failed" ? <AlertTriangle className="size-4 shrink-0 text-destructive" /> : null}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Badge tone={statusTone(data.status)}>{formatRunStatus(data.status)}</Badge>
        <span className="text-xs text-muted-foreground">{formatDuration(data.durationMs)}</span>
      </div>
    </div>
  )
}
