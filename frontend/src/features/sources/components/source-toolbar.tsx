"use client"

import { StudioToolbar } from "@/features/studio/shared/components/studio-dashboard"
import { StudioFilterRow, StudioSearchFilter, StudioSelectFilter } from "@/features/studio/shared/components/studio-filters"
import { formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { SourceType } from "@/types/common"
import type { SourceFilters, SourceHealthStatus } from "@/types/source"

const sourceTypes: SourceType[] = [
  "official_blog",
  "rss",
  "atom",
  "github",
  "hackernews",
  "reddit",
  "arxiv",
  "lobsters",
  "stackoverflow",
  "devto",
  "medium",
  "html",
  "web_page",
  "manual",
  "media",
  "custom"
]

const healthStatuses: SourceHealthStatus[] = ["healthy", "degraded", "failed", "down", "cooling_down", "disabled"]

export function SourceToolbar({ filters, onChange }: { filters: SourceFilters; onChange: (filters: SourceFilters) => void }) {
  const { locale, t } = useI18n()
  const optionLabel = (value: string) => formatOption(locale, value)
  return (
    <StudioToolbar>
      <StudioFilterRow>
        <StudioSearchFilter
          className="sm:max-w-[320px]"
          placeholder={t("studio.sources.searchPlaceholder")}
          value={filters.keyword}
          onChange={(keyword) => onChange({ ...filters, keyword })}
        />
        <div className="flex flex-wrap items-end gap-2 md:ml-auto">
          <StudioSelectFilter
            label={t("studio.sources.type")}
            includeAll={false}
            value={filters.type}
            options={["all", ...sourceTypes]}
            optionLabel={optionLabel}
            onChange={(value) => onChange({ ...filters, type: value as SourceFilters["type"] })}
          />
          <StudioSelectFilter
            label={t("studio.sources.health")}
            includeAll={false}
            value={filters.healthStatus}
            options={["all", ...healthStatuses]}
            optionLabel={optionLabel}
            onChange={(value) => onChange({ ...filters, healthStatus: value as SourceFilters["healthStatus"] })}
          />
          <StudioSelectFilter
            label={t("studio.sources.enabled")}
            includeAll={false}
            value={filters.enabled}
            options={["all", "enabled", "disabled"]}
            optionLabel={optionLabel}
            onChange={(value) => onChange({ ...filters, enabled: value as SourceFilters["enabled"] })}
          />
        </div>
      </StudioFilterRow>
    </StudioToolbar>
  )
}

function formatOption(locale: "zh" | "en", value: string): string {
  if (value === "all") return locale === "zh" ? "全部" : "All"
  if (value === "enabled") return locale === "zh" ? "已启用" : "Enabled"
  if (value === "disabled") return locale === "zh" ? "已禁用" : "Disabled"
  const translatedStatus = formatStatus(locale, value)
  if (translatedStatus !== value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())) {
    return translatedStatus
  }
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ")
}
