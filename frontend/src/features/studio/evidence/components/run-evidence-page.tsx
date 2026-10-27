"use client"

import Link from "next/link"
import { ArrowLeft, CheckCircle2, CircleHelp, FileWarning, Gauge, Route, XCircle } from "lucide-react"
import { Badge } from "@/components/common/badge"
import { Button } from "@/components/ui/button"
import { ClaimSupportTable } from "@/features/studio/evidence/components/claim-support-table"
import { NoticeList } from "@/features/studio/evidence/components/evidence-center-page"
import { LlmTracePanel } from "@/features/studio/evidence/components/llm-trace-panel"
import { QualityLineageGraph } from "@/features/studio/evidence/components/quality-lineage-graph"
import { UnsupportedClaimsPanel } from "@/features/studio/evidence/components/unsupported-claims-panel"
import {
  StudioMetricCard,
  StudioMetricGrid,
  StudioPageHeader
} from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioRunEvidenceDetail } from "@/types/evidence"

export function RunEvidencePage({ detail }: { detail: StudioRunEvidenceDetail }) {
  const { t, dataState, status } = useI18n()
  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={t("studio.module.evidenceCenter.title")}
        title={detail.workflowName ?? detail.runId}
        description={t("studio.evidence.runDetailDescription")}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/studio/evidence">
              <ArrowLeft size={15} strokeWidth={1.8} aria-hidden="true" />
              {t("studio.evidence.backToEvidence")}
            </Link>
          </Button>
        }
        meta={
          <>
            <Badge tone={detail.dataState === "ready" ? "success" : detail.dataState === "fallback" ? "info" : "warning"}>{dataState(detail.dataState)}</Badge>
            {detail.qualityDecision ? <Badge tone={detail.qualityDecision === "blocked" ? "danger" : "neutral"}>{status(detail.qualityDecision)}</Badge> : null}
          </>
        }
      />
      <NoticeList notices={detail.notices} tone={detail.dataState === "ready" ? "success" : "warning"} />

      <StudioMetricGrid className="xl:grid-cols-5 2xl:grid-cols-5">
        <StudioMetricCard label={t("studio.runs.runId")} value={<span className="block truncate font-mono text-body2">{detail.runId}</span>} detail={detail.reportId ? `${t("studio.evidence.report")} ${detail.reportId}` : t("studio.evidence.noReportLinked")} icon={Route} />
        <StudioMetricCard label={t("studio.evidence.decision")} value={detail.qualityDecision ? status(detail.qualityDecision) : t("common.unknown")} detail={detail.qualityRoute ?? t("studio.evidence.noQualityRoute")} icon={Gauge} tone={detail.qualityDecision === "blocked" ? "danger" : "accent"} />
        <StudioMetricCard label={t("studio.evidence.accepted")} value={detail.counts.accepted} detail={t("studio.evidence.supported")} icon={CheckCircle2} tone="success" />
        <StudioMetricCard label={t("studio.evidence.rejected")} value={detail.counts.rejected} detail={t("studio.evidence.rejected")} icon={XCircle} tone="danger" />
        <StudioMetricCard label={t("studio.evidence.unsupported")} value={detail.counts.unsupported} detail={t("studio.evidence.uncertainCount", { count: detail.counts.uncertain })} icon={FileWarning} tone={detail.counts.unsupported ? "warning" : "info"} />
      </StudioMetricGrid>

      <StudioMetricGrid className="xl:grid-cols-4 2xl:grid-cols-4">
        <StudioMetricCard label={t("studio.evidence.qualityScore")} value={detail.qualityScore === undefined ? t("common.none") : `${detail.qualityScore}%`} detail={t("studio.evidence.reportQuality")} icon={Gauge} tone="accent" />
        <StudioMetricCard label={t("studio.evidence.uncertain")} value={detail.counts.uncertain} detail={t("studio.evidence.needsReview")} icon={CircleHelp} tone="warning" />
        <StudioMetricCard label={t("studio.evidence.rejected")} value={detail.counts.rejected} detail={t("studio.evidence.notUsable")} icon={XCircle} tone="danger" />
        <StudioMetricCard label={t("studio.evidence.claims")} value={detail.claims.length} detail={t("studio.evidence.renderedInSupportTable")} icon={Route} />
      </StudioMetricGrid>

      <QualityLineageGraph claims={detail.claims} />
      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <ClaimSupportTable claims={detail.claims} />
        <div className="space-y-3">
          <UnsupportedClaimsPanel claims={detail.claims} />
          <LlmTracePanel trace={detail.llmTrace} />
        </div>
      </section>
    </main>
  )
}
