import { EmptyState } from "@/components/common/empty-state"
import { ToolCallCard } from "@/features/studio/runs/components/tool-call-card"
import type { ToolCall } from "@/types/agent"

export function ToolCallList({ calls }: { calls: ToolCall[] }) {
  if (!calls.length) return <EmptyState title="暂无工具调用" description="这次运行没有调用工具。" />
  return (
    <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
      {calls.map((call) => (
        <ToolCallCard key={call.id} call={call} />
      ))}
    </div>
  )
}
