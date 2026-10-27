import { Badge } from "@/components/common/badge"
import { formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import type { AgentRunStatus } from "@/types/agent"

export function AgentRunStatusBadge({ status }: { status: AgentRunStatus }) {
  const label = formatRunStatus(status)
  return (
    <Badge tone={statusTone(status)}>
      {status === "running" ? <span aria-hidden="true" className="size-1.5 shrink-0 animate-pulse rounded-full bg-current" /> : null}
      {label}
    </Badge>
  )
}
