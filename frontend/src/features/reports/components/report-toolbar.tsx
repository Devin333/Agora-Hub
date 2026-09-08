"use client"

import { useI18n } from "@/lib/i18n/use-i18n"
import type { ReportFilters } from "@/features/reports/hooks/use-report-list"
import type { ReportStatus, ReportType } from "@/types/report"

const types: ReportType[] = ["daily", "weekly", "topic", "tech", "quality", "source_health"]
const statuses: ReportStatus[] = ["draft", "generated", "reviewed", "published", "failed"]

export function ReportToolbar({ filters, onChange }: { filters: ReportFilters; onChange: (filters: ReportFilters) => void }) {
  const { locale, status } = useI18n()
  const typeLabels: Record<ReportType, string> = {
    daily: locale === "zh" ? "日报" : "Daily",
    weekly: locale === "zh" ? "周报" : "Weekly",
    topic: locale === "zh" ? "主题" : "Topic",
    tech: locale === "zh" ? "技术" : "Technology",
    quality: locale === "zh" ? "质量" : "Quality",
    source_health: locale === "zh" ? "数据源健康" : "Source Health"
  }
  const statusLabels: Record<ReportStatus, string> = {
    draft: locale === "zh" ? "草稿" : "Draft",
    generated: locale === "zh" ? "已生成" : "Generated",
    reviewed: locale === "zh" ? "已复核" : "Reviewed",
    published: locale === "zh" ? "已发布" : "Published",
    failed: status("failed")
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="grid gap-3 md:grid-cols-[1fr_0.55fr_0.55fr]">
        <input
          aria-label={locale === "zh" ? "搜索报告" : "Search reports"}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:border-primary"
          placeholder={locale === "zh" ? "搜索报告..." : "Search reports..."}
          value={filters.keyword ?? ""}
          onChange={(event) => onChange({ ...filters, keyword: event.target.value })}
        />
        <select className="rounded-md border border-input bg-background px-3 py-2 text-sm" value={filters.reportType ?? ""} onChange={(event) => onChange({ ...filters, reportType: (event.target.value || undefined) as ReportType | undefined })}>
          <option value="">{locale === "zh" ? "全部报告类型" : "All report types"}</option>
          {types.map((type) => <option key={type} value={type}>{typeLabels[type]}</option>)}
        </select>
        <select className="rounded-md border border-input bg-background px-3 py-2 text-sm" value={filters.status ?? ""} onChange={(event) => onChange({ ...filters, status: (event.target.value || undefined) as ReportStatus | undefined })}>
          <option value="">{locale === "zh" ? "全部状态" : "All statuses"}</option>
          {statuses.map((item) => <option key={item} value={item}>{statusLabels[item]}</option>)}
        </select>
      </div>
    </div>
  )
}
