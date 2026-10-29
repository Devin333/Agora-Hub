"use client"

import Link from "next/link"
import { CheckCircle2, CircleHelp, FileWarning, Network, XCircle } from "lucide-react"
import { Badge } from "@/components/common/badge"
import {
  StudioMetricCard,
  StudioMetricGrid,
  StudioNotice,
  StudioPageHeader,
  StudioPanel
} from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioEvidenceOverview, StudioEvidenceRunSummary } from "@/types/evidence"

export function EvidenceCenterPage({ overview }: { overview: StudioEvidenceOverview }) {
  const { t } = useI18n()
  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={t("studio.nav.business")}
        title={t("studio.module.evidenceCenter.title")}
        description={t("studio.module.evidenceCenter.description")}
      />
      <NoticeList notices={overview.notices} tone={overview.dataState === "ready" ? "success" : "warning"} />
      <StudioMetricGrid className="xl:grid-cols-5 2xl:grid-cols-5">
        <StudioMetricCard label={t("studio.evidence.claims")} value={overview.totals.total} detail={t("studio.evidence.totalClaims")} icon={Network} tone="accent" />
        <StudioMetricCard label={t("studio.evidence.accepted")} value={overview.totals.accepted} detail={t("studio.evidence.supportedClaims")} icon={CheckCircle2} tone="success" />
        <StudioMetricCard label={t("studio.evidence.rejected")} value={overview.totals.rejected} detail={t("studio.evidence.rejectedClaims")} icon={XCircle} tone="danger" />
        <StudioMetricCard label={t("studio.evidence.uncertain")} value={overview.totals.uncertain} detail={t("studio.evidence.needsReview")} icon={CircleHelp} tone="warning" />
        <StudioMetricCard label={t("studio.evidence.unsupported")} value={overview.totals.unsupported} detail={t("studio.evidence.supportMissing")} icon={FileWarning} tone="info" />
      </StudioMetricGrid>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <StudioPanel title={t("studio.evidence.recentHealth")} description={t("studio.evidence.recentHealthDescription")}>
          {overview.runs.length ? (
            <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
              {overview.runs.map((run) => (
                <RunSummaryRow key={run.runId} run={run} />
              ))}
            </div>
          ) : (
            <p className="text-body2 text-muted-foreground">{t("studio.evidence.noRecentRuns")}</p>
          )}
        </StudioPanel>
        <StudioPanel title={t("studio.evidence.citationFailures")} description={t("studio.evidence.citationFailuresDescription")}>
          {overview.citationFailureCategories.length ? (
            <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
              {overview.citationFailureCategories.map((category) => (
                <div key={category.code} className="px-4 py-3">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-[13.5px] font-medium text-foreground">{category.label ?? category.code}</p>
                    <Badge tone="warning">{category.count}</Badge>
                  </div>
                  {category.items.length ? <p className="mt-1 text-label text-muted-foreground">{category.items.slice(0, 2).join("; ")}</p> : null}
                </div>
              ))}
            </div>
          ) : (
            <p className="rounded-xl border border-dashed border-border p-4 text-body2 text-muted-foreground">{t("studio.evidence.noCitationFailures")}</p>
          )}
        </StudioPanel>
      </section>
    </main>
  )
}

function RunSummaryRow({ run }: { run: StudioEvidenceRunSummary }) {
  const { t, dataState, status } = useI18n()
  const href = `/studio/evidence/runs/${encodeURIComponent(run.runId)}${run.reportId ? `?reportId=${encodeURIComponent(run.reportId)}` : ""}`
  return (
    <Link
      href={href}
      className="block px-4 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="break-words text-[13.5px] font-medium text-foreground">{run.workflowName ?? run.runId}</p>
          <p className="mt-0.5 break-words font-mono text-label text-muted-foreground">{run.runId}</p>
          {run.reportId ? <p className="mt-0.5 break-words text-label text-muted-foreground">{t("studio.evidence.report")} {run.reportId}</p> : null}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Badge tone={run.dataState === "ready" ? "success" : run.dataState === "fallback" ? "info" : "warning"}>{dataState(run.dataState)}</Badge>
          {run.qualityDecision ? <Badge tone={run.qualityDecision === "blocked" ? "danger" : "neutral"}>{status(run.qualityDecision)}</Badge> : null}
        </div>
      </div>
      <div className="mt-3 grid gap-2 text-label sm:grid-cols-4">
        <CountPill label={t("studio.evidence.accepted")} value={run.counts.accepted} />
        <CountPill label={t("studio.evidence.rejected")} value={run.counts.rejected} />
        <CountPill label={t("studio.evidence.uncertain")} value={run.counts.uncertain} />
        <CountPill label={t("studio.evidence.unsupported")} value={run.counts.unsupported} />
      </div>
    </Link>
  )
}

function CountPill({ label, value }: { label: string; value: number }) {
  return (
    <span className="rounded-lg bg-muted px-2 py-1 text-muted-foreground">
      {label}: <span className="font-semibold tabular-nums text-foreground">{value}</span>
    </span>
  )
}

export function NoticeList({ notices, tone = "warning" }: { notices: string[]; tone?: "success" | "warning" | "info" }) {
  const { t } = useI18n()
  if (!notices.length) return null
  return (
    <StudioNotice tone={tone} title={t("studio.evidence.dataNotice")}>
      <div className="flex flex-wrap gap-1.5">
        {notices.map((notice) => (
          <Badge key={notice} tone={tone}>
            {notice}
          </Badge>
        ))}
      </div>
    </StudioNotice>
  )
}
