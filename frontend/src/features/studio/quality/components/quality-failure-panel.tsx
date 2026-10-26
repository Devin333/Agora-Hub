import Link from "next/link"
import { AlertTriangle, Ban, ExternalLink } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Badge } from "@/components/ui/badge"
import { QualityStatusBadge } from "@/features/studio/quality/components/quality-check-table"
import { StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioBlockedRun, StudioQualityReportSummary } from "@/types/quality"

export function RecentFailedReports({ reports }: { reports: StudioQualityReportSummary[] }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.quality.recentFailedReports")} description={t("studio.quality.recentFailedReportsDescription")} contentClassName="space-y-3">
      {!reports.length ? (
        <EmptyState title={t("studio.quality.noFailedReports")} description={t("studio.quality.noFailedReportsDescription")} />
      ) : (
        reports.map((report) => (
          <Link
            key={report.reportId}
            href={`/studio/quality/reports/${encodeURIComponent(report.reportId)}`}
            className="block rounded-md border border-border bg-background p-3 hover:bg-secondary/50"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-foreground">{report.title}</p>
                <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{report.reportId}</p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <QualityStatusBadge status={report.status} />
                <ExternalLink className="size-4 text-muted-foreground" />
              </div>
            </div>
            {report.failureReasons.length ? (
              <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">{report.failureReasons[0]}</p>
            ) : null}
          </Link>
        ))
      )}
    </StudioPanel>
  )
}

export function BlockedRunPanel({ runs }: { runs: StudioBlockedRun[] }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.quality.blockedRuns")} description={t("studio.quality.blockedRunsDescription")} contentClassName="space-y-3">
      {!runs.length ? (
        <div className="rounded-md border border-success/30 bg-success/10 p-4">
          <div className="flex items-start gap-3">
            <Ban className="mt-0.5 size-5 text-success" />
            <div>
              <p className="text-sm font-medium text-success">{t("studio.quality.noBlockedRuns")}</p>
              <p className="mt-1 text-sm text-muted-foreground">{t("studio.quality.noBlockedRunsDescription")}</p>
            </div>
          </div>
        </div>
      ) : (
        runs.map((run) => (
          <div key={run.runId} className="rounded-md border border-warning/30 bg-warning/10 p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate font-mono text-sm font-medium text-foreground">{run.runId}</p>
                <p className="mt-1 text-sm text-muted-foreground">{run.summary}</p>
              </div>
              <AlertTriangle className="size-5 shrink-0 text-warning" />
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <QualityStatusBadge status={run.status} />
              {run.severity ? <Badge variant="warning">{run.severity}</Badge> : null}
              {run.latestEventCount !== undefined ? <Badge variant="muted">{t("studio.quality.events", { count: run.latestEventCount })}</Badge> : null}
            </div>
          </div>
        ))
      )}
    </StudioPanel>
  )
}

export function FailureReasonPanel({ reasons }: { reasons: string[] }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.quality.failureReasons")}>
      {!reasons.length ? (
        <p className="text-sm text-muted-foreground">{t("studio.quality.noFailureReason")}</p>
      ) : (
        <ul className="space-y-2">
          {reasons.map((reason) => (
            <li key={reason} className="flex gap-2 rounded-md border border-destructive/20 bg-destructive/10 p-3 text-sm">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
              <span className="text-foreground">{reason}</span>
            </li>
          ))}
        </ul>
      )}
    </StudioPanel>
  )
}
