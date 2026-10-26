import { Badge } from "@/components/common/badge"
import { formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import type { AgentRunStatus } from "@/types/agent"

export function AgentRunStatusBadge({ status }: { status: AgentRunStatus }) {
  const label = formatRunStatus(status)
  return (
    <Badge tone={statusTone(status)} className={status === "running" ? "relative pl-5" : undefined}>
      {status === "running" ? <span className="absolute left-2 size-1.5 rounded-full bg-link" /> : null}
      {label}
    </Badge>
  )
}
