"use client"

import Link from "next/link"
import { AlertTriangle, Archive, Bot, CheckCircle2, Gauge, Workflow } from "lucide-react"
import type { ReactNode } from "react"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { ScoreMeter } from "@/components/common/score-meter"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { Button } from "@/components/ui/button"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration } from "@/features/studio/runs/lib/run-format"
import { useStudioOverview } from "@/features/studio/hooks/use-studio-overview"
import {
  StudioMetricCard,
  StudioMetricGrid,
  StudioNotice,
  StudioPageHeader,
  StudioPanel,
  type StudioIcon
} from "@/features/studio/shared/components/studio-dashboard"
import type { StudioOverview } from "@/types/agent"

export function StudioOverviewDashboard({ overview, notices = [] }: { overview?: StudioOverview; notices?: string[] }) {
  const { data, isLoading, isError, error } = useStudioOverview(overview)

  if (isLoading) return <PageSkeleton />
  if (isError) return <ErrorState message={error instanceof Error ? error.message : "Studio 总览加载失败。"} />
  if (!data) return <EmptyState title="暂无 Studio 总览" description="运行时摘要数据不可用。" />

  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow="Studio"
        title="智能体运行总览"
        description="汇总活跃运行、近期失败、质量复核需求、产物、数据源健康和运行时错误。"
        actions={
          <Button asChild size="sm">
            <Link href="/studio/runs">打开智能体运行</Link>
          </Button>
        }
      />

      <StudioMetricGrid className="xl:grid-cols-4 2xl:grid-cols-4">
        <StudioMetricCard icon={Bot} label="活跃运行" value={String(data.activeRuns)} detail="当前运行中" tone="info" />
        <StudioMetricCard icon={AlertTriangle} label="24h 失败运行" value={String(data.failedRuns24h)} detail="失败或需要复核" tone="danger" />
        <StudioMetricCard icon={CheckCircle2} label="24h 完成" value={String(data.completedRuns24h)} detail="成功运行" tone="success" />
        <StudioMetricCard icon={Archive} label="产物" value={String(data.artifactsGenerated24h)} detail="近期运行生成" />
      </StudioMetricGrid>

      {notices.length ? (
        <StudioNotice>
          {notices.map((notice) => (
            <p key={notice}>{notice}</p>
          ))}
        </StudioNotice>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <StudioPanel
          title="最近智能体运行"
          description="Studio 工作流产生的近期运行证据。"
          actions={<Workflow size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />}
          flush
        >
          <div className="divide-y divide-border">
            {data.latestRuns.map((run) => (
              <Link
                key={run.id}
                href={`/studio/runs/${encodeURIComponent(run.id)}`}
                className="grid gap-3 px-4 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50 md:grid-cols-[minmax(0,1fr)_140px_120px] md:items-center"
              >
                <div className="min-w-0">
                  <p className="truncate text-[13.5px] font-medium text-foreground">{run.agentName}</p>
                  <p className="mt-0.5 truncate font-mono text-label text-muted-foreground">{run.id}</p>
                </div>
                <AgentRunStatusBadge status={run.status} />
                <p className="text-body2 tabular-nums text-muted-foreground">{formatDuration(run.durationMs)}</p>
              </Link>
            ))}
          </div>
        </StudioPanel>

        <aside className="space-y-3">
          <SidePanel title="运行时状态" icon={Gauge}>
            <p className="text-body2 text-muted-foreground">运行时正在读取 API 运行元数据，并用确定性 Studio 证据补齐不完整的可观测字段。</p>
          </SidePanel>
          <SidePanel title="需要质量复核" icon={AlertTriangle}>
            <p className="text-heading2 font-semibold tabular-nums text-warning-text">{data.qualityReviewRequired}</p>
            <p className="mt-1 text-label text-muted-foreground">发布前需要复核的运行。</p>
          </SidePanel>
          <SidePanel title="平均质量" icon={CheckCircle2}>
            <ScoreMeter value={data.avgQualityScore ?? 0} label="近期运行" />
          </SidePanel>
          <SidePanel title="错误摘要" icon={AlertTriangle}>
            <p className="text-body2 text-muted-foreground">引用覆盖不足是最近一次部分失败运行的主要失败模式。</p>
          </SidePanel>
        </aside>
      </section>
    </main>
  )
}

/** Compact rakazo framed panel with a leading icon for the overview side rail. */
function SidePanel({
  title,
  icon: Icon,
  children
}: {
  title: string
  icon: StudioIcon
  children: ReactNode
}) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />
        <h2 className="text-[15px] font-semibold text-foreground">{title}</h2>
      </div>
      {children}
    </section>
  )
}
