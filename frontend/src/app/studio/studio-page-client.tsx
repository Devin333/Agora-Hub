"use client"

import Link from "next/link"
import { AlertTriangle, Archive, CheckCircle2, Clock3, ShieldAlert, Workflow } from "lucide-react"
import { AgentRunStatusBadge } from "@/features/studio/runs/components/agent-run-status-badge"
import { formatDuration, shortRunId } from "@/features/studio/runs/lib/run-format"
import { StudioMetricCard, StudioMetricGrid, StudioNotice, StudioPageHeader, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import type { Translator } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import { formatDateTime } from "@/lib/format"
import type { StudioOverview, StudioRunListItem } from "@/types/agent"

export function StudioHomePageClient({ overview, notices = [] }: { overview: StudioOverview; notices?: string[] }) {
  const { t } = useI18n()
  const failedRuns = overview.latestRuns.filter((run) => run.errorCount > 0 || run.status === "failed" || run.status === "partially_failed")
  const sourceHealth = sourceHealthPreview(overview.latestRuns, t)

  return (
    <div className="space-y-6">
      <StudioPageHeader
        eyebrow={t("studio.dashboard.eyebrow")}
        title={t("studio.dashboard.title")}
        description={t("studio.dashboard.description")}
        actions={
          <Link className="inline-flex h-9 items-center gap-2 rounded-md border border-border bg-card px-3 text-sm font-medium text-foreground hover:bg-secondary" href="/studio/runs">
            <Workflow className="size-4" />
            {t("studio.module.runCenter.action")}
          </Link>
        }
      />

      <StudioMetricGrid className="xl:grid-cols-4 2xl:grid-cols-4">
        <StudioMetricCard label={t("studio.dashboard.activeRuns")} value={overview.activeRuns} detail={t("studio.dashboard.activeRunsDetail")} icon={Workflow} tone="info" />
        <StudioMetricCard label={t("studio.dashboard.completedRuns")} value={overview.completedRuns24h} detail={t("studio.dashboard.windowDetail")} icon={CheckCircle2} tone="success" />
        <StudioMetricCard label={t("studio.dashboard.failedRuns")} value={overview.failedRuns24h} detail={t("studio.dashboard.windowDetail")} icon={AlertTriangle} tone={overview.failedRuns24h ? "danger" : "neutral"} />
        <StudioMetricCard label={t("studio.dashboard.qualityReview")} value={overview.qualityReviewRequired} detail={t("studio.dashboard.qualityReviewDetail")} icon={ShieldAlert} tone={overview.qualityReviewRequired ? "warning" : "success"} />
        <StudioMetricCard label={t("studio.dashboard.artifacts")} value={overview.artifactsGenerated24h} detail={t("studio.dashboard.windowDetail")} icon={Archive} tone="accent" />
        <StudioMetricCard label={t("studio.dashboard.avgDuration")} value={formatDuration(overview.avgDurationMs)} detail={t("studio.dashboard.allRunsDetail")} icon={Clock3} />
        <StudioMetricCard label={t("studio.dashboard.avgQuality")} value={overview.avgQualityScore ?? "n/a"} detail={t("studio.dashboard.allRunsDetail")} tone="success" />
        <StudioMetricCard label={t("studio.dashboard.sourceHealth")} value={sourceHealth.value} detail={sourceHealth.detail} tone={sourceHealth.tone} />
      </StudioMetricGrid>

      {notices.length ? (
        <StudioNotice title={t("studio.runs.dataNotice")}>
          {notices.map((notice) => (
            <p key={notice}>{notice}</p>
          ))}
        </StudioNotice>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(320px,0.8fr)]">
        <StudioPanel title={t("studio.dashboard.latestRuns")} description={t("studio.dashboard.latestRunsDetail")} contentClassName="p-0">
          <div className="divide-y divide-border">
            {overview.latestRuns.map((run) => (
              <LatestRunRow key={run.id} run={run} />
            ))}
          </div>
        </StudioPanel>
        <StudioPanel title={t("studio.dashboard.errorSummary")} description={t("studio.dashboard.errorSummaryDetail")}>
          {failedRuns.length ? (
            <div className="space-y-3">
              {failedRuns.slice(0, 4).map((run) => (
                <Link key={run.id} href={`/studio/runs/${encodeURIComponent(run.id)}`} className="block rounded-md border border-border bg-secondary/30 p-3 hover:bg-secondary">
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate font-mono text-xs text-link">{shortRunId(run.id)}</span>
                    <AgentRunStatusBadge status={run.status} />
                  </div>
                  <p className="mt-2 text-sm font-medium text-foreground">{run.workflowName ?? run.agentName}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{run.errorCount} {t("studio.runs.errors")} · {run.qualityScore ?? "n/a"} {t("studio.dashboard.avgQuality")}</p>
                </Link>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">{t("studio.dashboard.noErrors")}</p>
          )}
        </StudioPanel>
      </section>
    </div>
  )
}

function LatestRunRow({ run }: { run: StudioRunListItem }) {
  const { t } = useI18n()
  return (
    <Link href={`/studio/runs/${encodeURIComponent(run.id)}`} className="grid gap-3 px-4 py-3 hover:bg-secondary/40 md:grid-cols-[minmax(160px,0.7fr)_minmax(220px,1fr)_120px_120px_90px] md:items-center">
      <div className="min-w-0">
        <p className="truncate font-mono text-xs font-medium text-link">{shortRunId(run.id)}</p>
        <p className="mt-1 truncate text-xs text-muted-foreground">{formatDateTime(run.startedAt)}</p>
      </div>
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-foreground">{run.workflowName ?? run.agentName}</p>
        <p className="mt-1 truncate text-xs text-muted-foreground">{run.agentName} · {run.profile}</p>
      </div>
      <AgentRunStatusBadge status={run.status} />
      <p className="text-sm text-muted-foreground">{formatDuration(run.durationMs)}</p>
      <p className={run.errorCount ? "text-sm font-semibold text-destructive" : "text-sm text-muted-foreground"}>{run.errorCount} {t("studio.runs.errors")}</p>
    </Link>
  )
}

function sourceHealthPreview(runs: StudioRunListItem[], t: Translator): { value: string; detail: string; tone: "success" | "warning" | "danger" } {
  const recentFailures = runs.filter((run) => run.status === "failed" || run.status === "partially_failed").length
  if (recentFailures >= 3) return { value: t("studio.dashboard.sourceHealth.degraded"), detail: t("studio.dashboard.sourceHealth.degradedDetail"), tone: "danger" }
  if (recentFailures > 0) return { value: t("studio.dashboard.sourceHealth.watch"), detail: t("studio.dashboard.sourceHealth.watchDetail"), tone: "warning" }
  return { value: t("studio.dashboard.sourceHealth.healthy"), detail: t("studio.dashboard.sourceHealth.healthyDetail"), tone: "success" }
}
