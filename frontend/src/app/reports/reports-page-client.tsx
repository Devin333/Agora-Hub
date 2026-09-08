"use client"

import { useMemo } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { PageHeader } from "@/components/layout/page-header"
import { ReportList } from "@/features/reports/components/report-list"
import { ReportPreparation } from "@/features/reports/components/report-preparation"
import { ReportToolbar } from "@/features/reports/components/report-toolbar"
import { useReportList, type ReportFilters } from "@/features/reports/hooks/use-report-list"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { ReportDraft } from "@/features/reports/lib/report-drafts"
import type { ReportStatus, ReportType } from "@/types/report"

const REPORT_TYPES: ReportType[] = ["daily", "weekly", "topic", "tech", "quality", "source_health"]
const REPORT_STATUSES: ReportStatus[] = ["draft", "generated", "reviewed", "published", "failed"]

export function ReportsPageClient() {
  const { t } = useI18n()
  const router = useRouter()
  const searchParams = useSearchParams()
  const question = searchParams.get("question") ?? ""
  const draftId = searchParams.get("draft")
  const compose = searchParams.get("compose") === "1"
  const filters = useMemo(() => reportFiltersFromParams(searchParams), [searchParams])
  const reports = useReportList(filters)

  const replaceParams = (mutate: (params: URLSearchParams) => void) => {
    const params = new URLSearchParams(searchParams.toString())
    mutate(params)
    router.replace(params.size ? `/reports?${params.toString()}` : "/reports", { scroll: false })
  }

  const setFilters = (next: ReportFilters) => replaceParams((params) => {
    setOrDelete(params, "q", next.keyword)
    setOrDelete(params, "reportType", next.reportType)
    setOrDelete(params, "status", next.status)
  })

  const rememberDraft = (draft: ReportDraft) => replaceParams((params) => {
    params.set("draft", draft.id)
    params.set("question", draft.question || draft.title)
    params.set("compose", "1")
    params.set("entry", "home")
  })

  return (
    <div className="space-y-6">
      <PageHeader eyebrow={t("portal.reports.eyebrow")} title={t("portal.reports.title")} description={t("portal.reports.description")} />
      <div className="flex items-center justify-between gap-4"><a href="/design-demo" className="rounded text-sm text-[#7c3aed] hover:underline focus-visible:ring-2 focus-visible:ring-[#8b5cf6]">返回首页</a>{!compose && <button type="button" onClick={() => replaceParams(params => params.set("compose", "1"))} className="rounded-xl border border-[#e0d3f2] bg-[#faf7ff] px-4 py-2.5 text-sm font-medium text-[#6d28d9] focus-visible:ring-2 focus-visible:ring-[#8b5cf6]">准备研究报告</button>}</div>
      {compose ? <ReportPreparation question={question} draftId={draftId} onSaved={rememberDraft} /> : question ? <QuestionContext question={question} /> : null}
      <h2 className="text-lg font-semibold">已有报告</h2>
      <ReportToolbar filters={filters} onChange={setFilters} />
      {reports.isLoading ? <PageSkeleton /> : null}
      {reports.isError ? <ErrorState title="报告加载失败" message={reports.error?.message} onRetry={reports.refetch} /> : null}
      {!reports.isLoading && !reports.isError ? <ReportList reports={reports.data} /> : null}
    </div>
  )
}

function QuestionContext({ question }: { question: string }) {
  return (
    <div className="rounded-xl border border-[#e7dff1] bg-[#faf7ff] px-4 py-3 text-sm text-[#695d7d]">
      来自首页的问题：<span className="font-medium text-[#382758]">{question}</span>
    </div>
  )
}

function reportFiltersFromParams(params: { get(name: string): string | null }): ReportFilters {
  const reportType = params.get("reportType")
  const status = params.get("status")
  return {
    keyword: params.get("q") ?? undefined,
    reportType: REPORT_TYPES.includes(reportType as ReportType) ? reportType as ReportType : undefined,
    status: REPORT_STATUSES.includes(status as ReportStatus) ? status as ReportStatus : undefined,
  }
}

function setOrDelete(params: URLSearchParams, key: string, value?: string) {
  const normalized = value?.trim()
  if (normalized) params.set(key, normalized)
  else params.delete(key)
}
