import { AlertTriangle } from "lucide-react"
import type { NodeProps } from "reactflow"
import { Badge } from "@/components/common/badge"
import { formatDuration, formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import type { WorkflowDagNode } from "@/types/agent"

export function WorkflowNode({ data }: NodeProps<WorkflowDagNode>) {
  return (
    <div className="min-w-48 rounded-xl border border-border bg-card px-3 py-2.5 text-card-foreground">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[13.5px] font-medium text-foreground">{data.label}</p>
          <p className="mt-0.5 text-label text-muted-foreground">{data.type}</p>
        </div>
        {data.status === "failed" ? <AlertTriangle size={16} strokeWidth={1.8} aria-hidden="true" className="shrink-0 text-destructive" /> : null}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Badge tone={statusTone(data.status)}>{formatRunStatus(data.status)}</Badge>
        <span className="text-label tabular-nums text-muted-foreground">{formatDuration(data.durationMs)}</span>
      </div>
    </div>
  )
}
