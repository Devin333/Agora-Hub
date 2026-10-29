"use client"

import Link from "next/link"
import { ArrowLeft, ExternalLink } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ReviewDecisionPanel } from "@/features/studio/review/components/review-decision-panel"
import { ReviewHistoryPanel } from "@/features/studio/review/components/review-history-panel"
import { ReviewRiskBadge } from "@/features/studio/review/components/review-risk-badge"
import {
  formatDateTime as formatLocalizedDateTime,
  type Translator
} from "@/lib/i18n"
import {
  StudioCodeBlock,
  StudioField,
  StudioFieldGrid,
  StudioNotice,
  StudioPageHeader,
  StudioPanel
} from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioReviewDetail, StudioReviewItem } from "@/types/review"

export function HumanReviewDetailPage({ detail }: { detail: StudioReviewDetail }) {
  const { t, status } = useI18n()
  const { item, notices } = detail

  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={t("studio.module.humanReview.title")}
        title={item.approvalId}
        description={item.requestedAction}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/studio/review">
              <ArrowLeft size={15} strokeWidth={1.8} aria-hidden="true" />
              {t("studio.review.review")}
            </Link>
          </Button>
        }
        meta={
          <>
            <ReviewRiskBadge riskLevel={item.riskLevel} />
            <Badge variant="info">{status(item.status)}</Badge>
            {item.rawStatus && item.rawStatus !== item.status ? <Badge variant="muted">{item.rawStatus}</Badge> : null}
          </>
        }
      />

      {notices.length || item.notices.length ? (
        <StudioNotice tone="warning" title={t("studio.review.dataNotice")}>
          {[...notices, ...item.notices].map((notice) => (
            <p key={notice}>{notice}</p>
          ))}
        </StudioNotice>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="space-y-3">
          <ReviewDetailSummary item={item} />
          <PayloadPreview payload={item.payloadPreview} />
          <ReviewHistoryPanel history={item.history} />
        </div>
        <ReviewDecisionPanel item={item} />
      </section>
    </main>
  )
}

function ReviewDetailSummary({ item }: { item: StudioReviewItem }) {
  const { locale, t } = useI18n()
  return (
    <StudioPanel title={t("studio.review.summary")} description={riskExplanation(item, t)}>
      <StudioFieldGrid className="md:grid-cols-2">
        <StudioField label={t("studio.review.requestedAction")} value={item.requestedAction} />
        <StudioField label={t("studio.review.requestedBy")} value={item.requestedBy ?? "n/a"} />
        <StudioField label={t("studio.review.requestedAt")} value={formatReviewDateTime(locale, item.requestedAt)} />
        <StudioField label={t("studio.review.expiresAt")} value={formatReviewDateTime(locale, item.expiresAt)} />
        <StudioField label={t("studio.review.runId")} value={item.runId ? <ResourceLink href={`/studio/runs/${encodeURIComponent(item.runId)}`} label={item.runId} /> : "n/a"} />
        <StudioField label={t("studio.review.reportId")} value={item.reportId ? <ResourceLink href={`/reports/${encodeURIComponent(item.reportId)}`} label={item.reportId} /> : "n/a"} />
      </StudioFieldGrid>
      {item.reason ? (
        <div className="mt-3 rounded-xl border border-border px-4 py-3">
          <h3 className="text-label font-medium text-muted-foreground">{t("studio.review.reason")}</h3>
          <p className="mt-1 text-body2 text-foreground">{item.reason}</p>
        </div>
      ) : null}
    </StudioPanel>
  )
}

function PayloadPreview({ payload }: { payload?: Record<string, unknown> }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.review.payloadPreview")}>
      <StudioCodeBlock className="max-h-[34rem]">{JSON.stringify(payload ?? {}, null, 2)}</StudioCodeBlock>
    </StudioPanel>
  )
}

function ResourceLink({ href, label }: { href: string; label: string }) {
  return (
    <Link className="inline-flex max-w-full items-center gap-1 truncate rounded-md text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50" href={href}>
      <span className="truncate">{label}</span>
      <ExternalLink size={13} strokeWidth={1.8} aria-hidden="true" className="shrink-0" />
    </Link>
  )
}

function riskExplanation(item: StudioReviewItem, t: Translator): string {
  if (item.riskLevel === "critical") return t("studio.review.criticalRisk")
  if (item.riskLevel === "high") return t("studio.review.highRisk")
  if (item.riskLevel === "medium") return t("studio.review.mediumRisk")
  return t("studio.review.lowRisk")
}

function formatReviewDateTime(locale: "zh" | "en", value: string | undefined): string | undefined {
  if (!value) return undefined
  return formatLocalizedDateTime(locale, value)
}
