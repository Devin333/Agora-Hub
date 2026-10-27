"use client"

import { useState } from "react"
import { ErrorState } from "@/components/common/error-state"
import { RunListTable } from "@/features/studio/runs/components/run-list-table"
import { RunStatusSummary } from "@/features/studio/runs/components/run-status-summary"
import { useRunList } from "@/features/studio/runs/hooks/use-run-list"
import { StudioNotice, StudioPageHeader, StudioToolbar } from "@/features/studio/shared/components/studio-dashboard"
import {
  StudioCheckboxFilter,
  StudioFilterRow,
  StudioNumberFilter,
  StudioSearchFilter,
  StudioSelectFilter
} from "@/features/studio/shared/components/studio-filters"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { AgentRunFilters, AgentRunStatus, StudioRunListItem } from "@/types/agent"

const statuses: AgentRunStatus[] = ["pending", "running", "success", "failed", "partially_failed", "blocked", "waiting_for_human", "cancelled"]

export function RunCenterPage({ runs, notices = [] }: { runs: StudioRunListItem[]; notices?: string[] }) {
  const { t, status } = useI18n()
  const [filters, setFilters] = useState<AgentRunFilters>({ sort: "startedAt" })
  const { data, isError, error } = useRunList(filters, runs)
  const agents = unique(runs.map((run) => run.agentName))
  const workflows = unique(runs.map((run) => run.workflowId ?? run.workflowName ?? run.agentName))
  const profiles = unique(runs.map((run) => run.profile))

  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={t("studio.nav.runtime")}
        title={t("studio.module.runCenter.title")}
        description={t("studio.module.runCenter.description")}
      />
      {notices.length ? (
        <StudioNotice title={t("studio.runs.dataNotice")}>
          {notices.map((notice) => (
            <p key={notice}>{notice}</p>
          ))}
        </StudioNotice>
      ) : null}
      <RunStatusSummary runs={runs} />
      <StudioToolbar className="space-y-2">
        <StudioFilterRow>
          <StudioSearchFilter
            placeholder={t("studio.runs.searchPlaceholder")}
            value={filters.keyword ?? ""}
            onChange={(value) => setFilters({ ...filters, keyword: value || undefined })}
          />
          <StudioSelectFilter label={t("studio.runs.agent")} value={filters.agentName?.[0] ?? ""} options={agents} allLabel={t("studio.runs.all")} onChange={(value) => setFilters({ ...filters, agentName: value ? [value] : undefined })} />
          <StudioSelectFilter label={t("studio.runs.workflow")} value={filters.workflowId?.[0] ?? ""} options={workflows} allLabel={t("studio.runs.all")} onChange={(value) => setFilters({ ...filters, workflowId: value ? [value] : undefined })} />
          <StudioSelectFilter label={t("common.status")} value={filters.status?.[0] ?? ""} options={statuses} allLabel={t("studio.runs.all")} optionLabel={(value) => status(value)} onChange={(value) => setFilters({ ...filters, status: value ? [value as AgentRunStatus] : undefined })} />
          <StudioSelectFilter label={t("studio.runs.profile")} value={filters.profile?.[0] ?? ""} options={profiles} allLabel={t("studio.runs.all")} onChange={(value) => setFilters({ ...filters, profile: value ? [value] : undefined })} />
          <StudioSelectFilter
            label={t("studio.runs.range")}
            value={filters.dateRange ?? ""}
            options={["today", "week", "month"]}
            allLabel={t("studio.runs.all")}
            optionLabel={(value) => t(`studio.runs.range.${value}`)}
            onChange={(value) => setFilters({ ...filters, dateRange: value ? (value as AgentRunFilters["dateRange"]) : undefined })}
          />
          <StudioSelectFilter
            label={t("studio.runs.sort")}
            value={filters.sort ?? "startedAt"}
            options={["startedAt", "durationMs", "qualityScore", "errorCount"]}
            includeAll={false}
            optionLabel={(value) => t(`studio.runs.sort.${value}`)}
            onChange={(value) => setFilters({ ...filters, sort: value as AgentRunFilters["sort"] })}
          />
        </StudioFilterRow>
        <StudioFilterRow>
          <StudioCheckboxFilter label={t("studio.runs.errorsOnly")} checked={Boolean(filters.hasError)} onChange={(checked) => setFilters({ ...filters, hasError: checked || undefined })} />
          <StudioNumberFilter label={t("studio.runs.minimumQuality")} min={0} max={100} value={filters.minQualityScore} onChange={(value) => setFilters({ ...filters, minQualityScore: value })} />
        </StudioFilterRow>
      </StudioToolbar>
      {isError ? <ErrorState message={error instanceof Error ? error.message : t("studio.runs.listFailed")} /> : <RunListTable runs={data} />}
    </main>
  )
}

function unique(values: Array<string | undefined>): string[] {
  return Array.from(new Set(values.filter((value): value is string => Boolean(value)))).sort()
}
