"use client"

import Link from "next/link"
import { AlertTriangle, Archive, Bot, CheckCircle2, Gauge, Workflow } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { ScoreMeter } from "@/components/common/score-meter"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { PageHeader } from "@/components/layout/page-header"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration } from "@/features/studio/runs/lib/run-format"
import { useStudioOverview } from "@/features/studio/hooks/use-studio-overview"
import type { StudioOverview } from "@/types/agent"

export function StudioOverviewDashboard({ overview, notices = [] }: { overview?: StudioOverview; notices?: string[] }) {
  const { data, isLoading, isError, error } = useStudioOverview(overview)

  if (isLoading) return <PageSkeleton />
  if (isError) return <ErrorState message={error instanceof Error ? error.message : "Studio 总览加载失败。"} />
  if (!data) return <EmptyState title="暂无 Studio 总览" description="运行时摘要数据不可用。" />

  return (
    <main className="space-y-6">
      <PageHeader
        eyebrow="Studio"
        title="智能体运行总览"
        description="汇总活跃运行、近期失败、质量复核需求、产物、数据源健康和运行时错误。"
        actions={
          <Link className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90" href="/studio/runs">
            打开智能体运行
          </Link>
        }
      />

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <SummaryCard icon={Bot} label="活跃运行" value={String(data.activeRuns)} detail="当前运行中" />
        <SummaryCard icon={AlertTriangle} label="24h 失败运行" value={String(data.failedRuns24h)} detail="失败或需要复核" tone="danger" />
        <SummaryCard icon={CheckCircle2} label="24h 完成" value={String(data.completedRuns24h)} detail="成功运行" tone="success" />
        <SummaryCard icon={Archive} label="产物" value={String(data.artifactsGenerated24h)} detail="近期运行生成" />
      </section>

      {notices.length ? (
        <section className="rounded-lg border border-warning/30 bg-warning/10 p-4">
          {notices.map((notice) => (
            <p key={notice} className="text-sm text-warning">
              {notice}
            </p>
          ))}
        </section>
      ) : null}

      <section className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="rounded-lg border border-border bg-card">
          <div className="flex items-center justify-between gap-3 border-b border-border p-4">
            <div>
              <h2 className="text-base font-semibold text-foreground">最近智能体运行</h2>
              <p className="text-sm text-muted-foreground">Studio 工作流产生的近期运行证据。</p>
            </div>
            <Workflow className="size-5 text-link" />
          </div>
          <div className="divide-y divide-border">
            {data.latestRuns.map((run) => (
              <Link key={run.id} href={`/studio/runs/${encodeURIComponent(run.id)}`} className="grid gap-3 p-4 hover:bg-secondary md:grid-cols-[minmax(0,1fr)_140px_120px] md:items-center">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-foreground">{run.agentName}</p>
                  <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{run.id}</p>
                </div>
                <AgentRunStatusBadge status={run.status} />
                <p className="text-sm text-muted-foreground">{formatDuration(run.durationMs)}</p>
              </Link>
            ))}
          </div>
        </div>

        <aside className="space-y-4">
          <Panel title="运行时状态" icon={Gauge}>
            <p className="text-sm text-muted-foreground">运行时正在读取 API 运行元数据，并用确定性 Studio 证据补齐不完整的可观测字段。</p>
          </Panel>
          <Panel title="需要质量复核" icon={AlertTriangle}>
            <p className="text-3xl font-semibold text-warning">{data.qualityReviewRequired}</p>
            <p className="mt-1 text-sm text-muted-foreground">发布前需要复核的运行。</p>
          </Panel>
          <Panel title="平均质量" icon={CheckCircle2}>
            <ScoreMeter value={data.avgQualityScore ?? 0} label="近期运行" />
          </Panel>
          <Panel title="错误摘要" icon={AlertTriangle}>
            <p className="text-sm text-muted-foreground">引用覆盖不足是最近一次部分失败运行的主要失败模式。</p>
          </Panel>
        </aside>
      </section>
    </main>
  )
}

function SummaryCard({
  icon: Icon,
  label,
  value,
  detail,
  tone = "accent"
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: string
  detail: string
  tone?: "accent" | "success" | "danger"
}) {
  const color = tone === "success" ? "text-success" : tone === "danger" ? "text-destructive" : "text-link"
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs uppercase tracking-normal text-muted-foreground">{label}</p>
        <Icon className={`size-4 ${color}`} />
      </div>
      <p className={`mt-3 text-3xl font-semibold ${color}`}>{value}</p>
      <p className="mt-1 text-sm text-muted-foreground">{detail}</p>
    </div>
  )
}

function Panel({ title, icon: Icon, children }: { title: string; icon: React.ComponentType<{ className?: string }>; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon className="size-4 text-link" />
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      </div>
      {children}
    </section>
  )
}
