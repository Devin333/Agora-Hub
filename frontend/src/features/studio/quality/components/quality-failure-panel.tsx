import Link from "next/link"
import { AlertTriangle, ExternalLink } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Badge } from "@/components/ui/badge"
import { QualityStatusBadge } from "@/features/studio/quality/components/quality-check-table"
import { StudioNotice, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioBlockedRun, StudioQualityReportSummary } from "@/types/quality"

const LIST_CLASS = "divide-y divide-border overflow-hidden rounded-xl border border-border"

export function RecentFailedReports({ reports }: { reports: StudioQualityReportSummary[] }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.quality.recentFailedReports")} description={t("studio.quality.recentFailedReportsDescription")}>
      {!reports.length ? (
        <EmptyState title={t("studio.quality.noFailedReports")} description={t("studio.quality.noFailedReportsDescription")} />
      ) : (
        <div className={LIST_CLASS}>
          {reports.map((report) => (
            <Link
              key={report.reportId}
              href={`/studio/quality/reports/${encodeURIComponent(report.reportId)}`}
              className="block px-4 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-[13.5px] font-medium text-foreground">{report.title}</p>
                  <p className="mt-0.5 truncate font-mono text-label text-muted-foreground">{report.reportId}</p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <QualityStatusBadge status={report.status} />
                  <ExternalLink size={15} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />
                </div>
              </div>
              {report.failureReasons.length ? <p className="mt-1.5 line-clamp-2 text-body2 text-muted-foreground">{report.failureReasons[0]}</p> : null}
            </Link>
          ))}
        </div>
      )}
    </StudioPanel>
  )
}

export function BlockedRunPanel({ runs }: { runs: StudioBlockedRun[] }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.quality.blockedRuns")} description={t("studio.quality.blockedRunsDescription")}>
      {!runs.length ? (
        <StudioNotice tone="success" title={t("studio.quality.noBlockedRuns")}>
          {t("studio.quality.noBlockedRunsDescription")}
        </StudioNotice>
      ) : (
        <div className={LIST_CLASS}>
          {runs.map((run) => (
            <div key={run.runId} className="px-4 py-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-mono text-[13px] font-medium text-foreground">{run.runId}</p>
                  <p className="mt-0.5 text-body2 text-muted-foreground">{run.summary}</p>
                </div>
                <AlertTriangle size={16} strokeWidth={1.8} aria-hidden="true" className="shrink-0 text-warning" />
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                <QualityStatusBadge status={run.status} />
                {run.severity ? <Badge variant="warning">{run.severity}</Badge> : null}
                {run.latestEventCount !== undefined ? <Badge variant="muted">{t("studio.quality.events", { count: run.latestEventCount })}</Badge> : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </StudioPanel>
  )
}

export function FailureReasonPanel({ reasons }: { reasons: string[] }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.quality.failureReasons")}>
      {!reasons.length ? (
        <p className="text-body2 text-muted-foreground">{t("studio.quality.noFailureReason")}</p>
      ) : (
        <ul className={LIST_CLASS}>
          {reasons.map((reason) => (
            <li key={reason} className="flex gap-2 px-4 py-3 text-body2">
              <AlertTriangle size={16} strokeWidth={1.8} aria-hidden="true" className="mt-0.5 shrink-0 text-destructive" />
              <span className="text-foreground">{reason}</span>
            </li>
          ))}
        </ul>
      )}
    </StudioPanel>
  )
}
