import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { ScoreMeter } from "@/components/common/score-meter"
import type { RunQualitySummary } from "@/types/agent"

const tone = {
  passed: "success",
  warning: "warning",
  failed: "danger",
  review_required: "warning"
} as const

export function RunQualityPanel({ quality }: { quality?: RunQualitySummary }) {
  if (!quality) return <EmptyState title="暂无质量摘要" description="这次运行没有返回质量检查。" />

  return (
    <section className="space-y-3">
      <div className="rounded-xl border border-border px-4 py-3">
        <div className="mb-3 flex items-center justify-between gap-3">
          <Badge tone={tone[quality.status]}>{labelStatus(quality.status)}</Badge>
          <span className="text-label tabular-nums text-muted-foreground">{quality.checks.length} 项检查</span>
        </div>
        <ScoreMeter label="质量分" value={quality.score ?? 0} />
      </div>
      <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
        {quality.checks.map((check) => (
          <article key={check.id} className="px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-[13.5px] font-medium text-foreground">{check.name}</h3>
              <Badge tone={tone[check.status]}>{labelStatus(check.status)}</Badge>
            </div>
            {check.message ? <p className="mt-1 text-label text-muted-foreground">{check.message}</p> : null}
          </article>
        ))}
      </div>
    </section>
  )
}

function labelStatus(status: string) {
  const labels: Record<string, string> = {
    passed: "通过",
    warning: "警告",
    failed: "失败",
    review_required: "需要复核"
  }
  return labels[status] ?? status
}
