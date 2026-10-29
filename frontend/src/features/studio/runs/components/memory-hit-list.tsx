import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { titleCase } from "@/lib/format"
import type { MemoryHit } from "@/types/agent"

export function MemoryHitList({ hits }: { hits: MemoryHit[] }) {
  if (!hits.length) return <EmptyState title="暂无记忆命中" description="这次运行的记忆召回没有返回上下文。" />

  return (
    <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
      {hits.map((hit) => (
        <article key={hit.id} className="px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap gap-2">
              <Badge tone="accent">{titleCase(hit.memoryType)}</Badge>
              <Badge tone="neutral">{hit.memoryId}</Badge>
            </div>
            <span className="text-label tabular-nums text-muted-foreground">{hit.score}% 匹配</span>
          </div>
          <p className="mt-2 text-body2 text-foreground">{hit.summary}</p>
          {hit.relatedTopicName ? <p className="mt-1 text-label text-muted-foreground">主题：{hit.relatedTopicName}</p> : null}
        </article>
      ))}
    </div>
  )
}
