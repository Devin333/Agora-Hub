"use client"

import {
  StudioCheckboxFilter,
  StudioFilterRow,
  StudioNumberFilter,
  StudioSearchFilter,
  StudioSelectFilter
} from "@/features/studio/shared/components/studio-filters"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { AgentRun, AgentRunFilters, AgentRunStatus } from "@/types/agent"

const statuses: AgentRunStatus[] = ["running", "success", "failed", "partially_failed", "cancelled"]

export function AgentRunToolbar({
  runs,
  filters,
  onFiltersChange
}: {
  runs: AgentRun[]
  filters: AgentRunFilters
  onFiltersChange: (filters: AgentRunFilters) => void
}) {
  const { t, status } = useI18n()
  const agents = Array.from(new Set(runs.map((run) => run.agentName))).sort()
  const profiles = Array.from(new Set(runs.map((run) => run.profile))).sort()

  return (
    <section className="space-y-2">
      <StudioFilterRow>
        <StudioSearchFilter
          placeholder={t("studio.runs.searchPlaceholder")}
          value={filters.keyword ?? ""}
          onChange={(value) => onFiltersChange({ ...filters, keyword: value || undefined })}
        />
        <StudioSelectFilter
          label={t("studio.runs.agent")}
          value={filters.agentName?.[0] ?? ""}
          options={agents}
          allLabel={t("studio.runs.all")}
          onChange={(value) => onFiltersChange({ ...filters, agentName: value ? [value] : undefined })}
        />
        <StudioSelectFilter
          label={t("common.status")}
          value={filters.status?.[0] ?? ""}
          options={statuses}
          allLabel={t("studio.runs.all")}
          optionLabel={(value) => status(value)}
          onChange={(value) => onFiltersChange({ ...filters, status: value ? [value as AgentRunStatus] : undefined })}
        />
        <StudioSelectFilter
          label={t("studio.runs.profile")}
          value={filters.profile?.[0] ?? ""}
          options={profiles}
          allLabel={t("studio.runs.all")}
          onChange={(value) => onFiltersChange({ ...filters, profile: value ? [value] : undefined })}
        />
        <StudioSelectFilter
          label={t("studio.runs.range")}
          value={filters.dateRange ?? ""}
          options={["today", "week", "month"]}
          allLabel={t("studio.runs.all")}
          optionLabel={(value) => {
            if (value === "today") return t("common.today")
            if (value === "week") return t("common.thisWeek")
            if (value === "month") return t("common.thisMonth")
            return value
          }}
          onChange={(value) => onFiltersChange({ ...filters, dateRange: value ? (value as AgentRunFilters["dateRange"]) : undefined })}
        />
        <StudioSelectFilter
          label={t("studio.runs.sort")}
          value={filters.sort ?? "startedAt"}
          options={["startedAt", "durationMs", "qualityScore", "errorCount"]}
          includeAll={false}
          optionLabel={(value) => t(`studio.runs.sort.${value}`)}
          onChange={(value) => onFiltersChange({ ...filters, sort: value as AgentRunFilters["sort"] })}
        />
      </StudioFilterRow>
      <StudioFilterRow>
        <StudioCheckboxFilter
          label={t("studio.runs.errorsOnly")}
          checked={Boolean(filters.hasError)}
          onChange={(checked) => onFiltersChange({ ...filters, hasError: checked || undefined })}
        />
        <StudioNumberFilter
          label={t("studio.runs.minimumQuality")}
          min={0}
          max={100}
          value={filters.minQualityScore}
          onChange={(value) => onFiltersChange({ ...filters, minQualityScore: value })}
        />
      </StudioFilterRow>
    </section>
  )
}
