"use client"

import { FormEvent, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, BarChart3, Database, FileSearch, GitBranch, Play, RefreshCw, ShieldAlert, Sparkles, Tags, Wrench } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ApiRequestError } from "@/lib/api/client"
import { formatDataState, formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import { queryKeys } from "@/lib/query/query-keys"
import {
  fetchPaperIngestOpsState,
  fetchPaperReaderOpsStats,
  refreshPaperReaderSummary,
  triggerPaperIngest,
  triggerPaperVisualCompileBackfill,
} from "@/features/studio/shared/api/paper-reader-ops-api"
import {
  StudioEmptyBlock,
  StudioMetricCard,
  StudioMetricGrid,
  StudioNotice,
  StudioPanel,
} from "@/features/studio/shared/components/studio-dashboard"
import type { Locale } from "@/lib/papers/types"
import type { PaperIngestOpsState, PaperReaderOpsStats } from "@/types/studio"

export function PaperReaderOpsPanel({ windowHours = 24 }: { windowHours?: number }) {
  const { locale: uiLocale, t, dateTime } = useI18n()
  const queryClient = useQueryClient()
  const [paperId, setPaperId] = useState("")
  const [summaryLocale, setSummaryLocale] = useState<Locale>("en")
  const [reason, setReason] = useState("")
  const [backfillLimit, setBackfillLimit] = useState("")
  const [backfillForce, setBackfillForce] = useState(false)
  const ingestLimit = 20

  const { data, error, isError, isLoading } = useQuery({
    queryKey: queryKeys.studio.paperReaderOpsStats(windowHours),
    queryFn: () => fetchPaperReaderOpsStats(windowHours),
    retry: false,
    staleTime: 30_000
  })

  const ingestQuery = useQuery({
    queryKey: queryKeys.studio.paperIngestOps(ingestLimit),
    queryFn: () => fetchPaperIngestOpsState(ingestLimit),
    retry: false,
    staleTime: 30_000
  })

  const refreshMutation = useMutation({
    mutationFn: () =>
      refreshPaperReaderSummary({
        paperId: paperId.trim(),
        locale: summaryLocale,
        reason: reason.trim(),
      }),
    onSuccess: () => {
      setReason("")
      void queryClient.invalidateQueries({ queryKey: queryKeys.studio.paperReaderOpsStats(windowHours) })
    }
  })

  const triggerIngestMutation = useMutation({
    mutationFn: () => triggerPaperIngest({}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.studio.paperIngestOps(ingestLimit) })
    }
  })

  const triggerVisualCompileBackfillMutation = useMutation({
    mutationFn: () =>
      triggerPaperVisualCompileBackfill({
        limit: parseOptionalPositiveInt(backfillLimit),
        force: backfillForce,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.studio.paperReaderOpsStats(windowHours) })
    }
  })

  const writeDisabled =
    isError ||
    data?.dataState === "fallback" ||
    !paperId.trim() ||
    !reason.trim() ||
    refreshMutation.isPending

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!writeDisabled) {
      refreshMutation.mutate()
    }
  }

  if (isLoading) {
    return (
      <StudioPanel title={t("studio.ops.title")} description={t("studio.ops.loading")}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {[0, 1, 2, 3, 4].map((item) => (
            <div key={item} className="h-24 animate-pulse rounded-md border border-border bg-secondary/50" />
          ))}
        </div>
      </StudioPanel>
    )
  }

  if (isError) {
    return (
      <StudioNotice tone="danger" title={t("studio.ops.unavailable")}>
        <p>{error instanceof Error ? error.message : t("studio.ops.requestFailed")}</p>
        {error instanceof ApiRequestError && error.requestId ? (
          <p className="mt-1 font-mono text-xs">requestId={error.requestId}</p>
        ) : null}
      </StudioNotice>
    )
  }

  if (!data || data.dataState === "empty") {
    return (
      <StudioPanel title={t("studio.ops.title")} description={t("studio.ops.window", { hours: windowHours })}>
        <div className="space-y-4">
          <StudioEmptyBlock
            title={t("studio.ops.emptyTitle")}
            description={t("studio.ops.emptyDescription")}
          />
          <SummaryRefreshForm
            paperId={paperId}
            locale={summaryLocale}
            reason={reason}
            disabled={writeDisabled}
            pending={refreshMutation.isPending}
            error={refreshMutation.error}
            success={refreshMutation.isSuccess}
            onPaperIdChange={setPaperId}
            onLocaleChange={setSummaryLocale}
            onReasonChange={setReason}
            onSubmit={handleSubmit}
          />
          <PaperIngestOpsContent
            state={ingestQuery.data}
            loading={ingestQuery.isLoading}
            error={ingestQuery.error}
            triggering={triggerIngestMutation.isPending}
            triggerError={triggerIngestMutation.error}
            triggerSuccess={triggerIngestMutation.isSuccess}
            onTrigger={() => triggerIngestMutation.mutate()}
          />
          <PaperVisualCompileBackfillContent
            limit={backfillLimit}
            force={backfillForce}
            triggering={triggerVisualCompileBackfillMutation.isPending}
            triggerError={triggerVisualCompileBackfillMutation.error}
            triggerSuccess={triggerVisualCompileBackfillMutation.isSuccess}
            onLimitChange={setBackfillLimit}
            onForceChange={setBackfillForce}
            onTrigger={() => triggerVisualCompileBackfillMutation.mutate()}
          />
        </div>
      </StudioPanel>
    )
  }

  return (
    <StudioPanel
      title={t("studio.ops.title")}
      description={t("studio.ops.windowEnding", { hours: data.windowHours, time: dateTime(data.windowEnd) })}
      actions={<Badge variant={data.dataState === "ready" ? "success" : "warning"}>{formatDataState(uiLocale, data.dataState)}</Badge>}
    >
      <div className="space-y-4">
        <PaperReaderOpsContent stats={data} />
        <PaperIngestOpsContent
          state={ingestQuery.data}
          loading={ingestQuery.isLoading}
          error={ingestQuery.error}
          triggering={triggerIngestMutation.isPending}
          triggerError={triggerIngestMutation.error}
          triggerSuccess={triggerIngestMutation.isSuccess}
          onTrigger={() => triggerIngestMutation.mutate()}
        />
        <PaperVisualCompileBackfillContent
          limit={backfillLimit}
          force={backfillForce}
          triggering={triggerVisualCompileBackfillMutation.isPending}
          triggerError={triggerVisualCompileBackfillMutation.error}
          triggerSuccess={triggerVisualCompileBackfillMutation.isSuccess}
          onLimitChange={setBackfillLimit}
          onForceChange={setBackfillForce}
          onTrigger={() => triggerVisualCompileBackfillMutation.mutate()}
        />
        <SummaryRefreshForm
          paperId={paperId}
          locale={summaryLocale}
          reason={reason}
          disabled={writeDisabled}
          pending={refreshMutation.isPending}
          error={refreshMutation.error}
          success={refreshMutation.isSuccess}
          onPaperIdChange={setPaperId}
          onLocaleChange={setSummaryLocale}
          onReasonChange={setReason}
          onSubmit={handleSubmit}
        />
      </div>
    </StudioPanel>
  )
}

function PaperReaderOpsContent({ stats }: { stats: PaperReaderOpsStats }) {
  const { locale, t, dateTime } = useI18n()
  const notices = useMemo(() => opsNotices(stats, t), [stats, t])
  const topError = Object.entries(stats.summaryEvents.errorCodeCounts).sort(([, left], [, right]) => right - left)[0]
  return (
    <div className="space-y-4">
      {notices.length ? (
        <StudioNotice tone={stats.dataState === "partial" ? "warning" : "info"} title={t("studio.ops.notice")}>
          <div className="space-y-1">
            {notices.map((notice) => (
              <p key={notice}>{notice}</p>
            ))}
          </div>
        </StudioNotice>
      ) : null}

      <StudioMetricGrid className="xl:grid-cols-5 2xl:grid-cols-5">
        <StudioMetricCard label={t("studio.ops.papers")} value={stats.paperCache.paperCount} detail={stats.paperCache.source ?? formatStatus(locale, stats.paperCache.status)} icon={Database} tone="accent" />
        <StudioMetricCard label={t("studio.ops.hitRate")} value={`${Math.round(stats.summaryEvents.hitRate * 100)}%`} detail={t("studio.ops.cacheHits", { count: stats.summaryEvents.cacheHitCount })} icon={BarChart3} tone={stats.summaryEvents.hitRate > 0 ? "success" : "neutral"} />
        <StudioMetricCard label={t("studio.ops.generated")} value={stats.summaryEvents.generatedCount} detail={t("studio.ops.v2CacheEntries", { count: stats.summaryCache.v2EntryCount })} icon={Sparkles} tone="info" />
        <StudioMetricCard label={t("studio.ops.failures")} value={stats.summaryEvents.failureCount} detail={topError ? `${topError[0]} (${topError[1]})` : t("studio.ops.noFailures")} icon={AlertTriangle} tone={stats.summaryEvents.failureCount ? "warning" : "success"} />
        <StudioMetricCard label={t("studio.ops.readerCache")} value={stats.readerCache.fileCount} detail={t("studio.ops.textArtifacts", { count: stats.textExtraction.fileCount })} icon={FileSearch} tone={stats.textExtraction.fileCount ? "success" : "neutral"} />
      </StudioMetricGrid>

      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="rounded-md border border-border bg-background p-3">
          <h3 className="text-sm font-semibold text-foreground">{t("studio.ops.cacheFreshness")}</h3>
          <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
            <StatusRow label={t("studio.ops.paperCache")} status={stats.paperCache.status} value={stats.paperCache.collectedAt ?? stats.paperCache.lastUpdatedAt} />
            <StatusRow label={t("studio.ops.summaryCache")} status={stats.summaryCache.status} value={stats.summaryCache.lastGeneratedAt ?? stats.summaryCache.lastUpdatedAt} />
            <StatusRow label={t("studio.ops.readerCache")} status={stats.readerCache.status} value={stats.readerCache.lastUpdatedAt} />
            <StatusRow label={t("studio.ops.textExtraction")} status={stats.textExtraction.status} value={stats.textExtraction.lastUpdatedAt} />
          </div>
        </div>

        <div className="rounded-md border border-border bg-background p-3">
          <h3 className="text-sm font-semibold text-foreground">{t("studio.ops.recentSummaryFailures")}</h3>
          <div className="mt-2 space-y-2">
            {stats.summaryEvents.recentFailures.length ? (
              stats.summaryEvents.recentFailures.map((item) => (
                <div key={`${item.timestamp}-${item.paperId}-${item.errorCode}`} className="rounded-md bg-secondary/40 px-3 py-2 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-semibold text-warning">{item.errorCode ?? "paper_summary_unavailable"}</span>
                    <span className="shrink-0 text-muted-foreground">{dateTime(item.timestamp)}</span>
                  </div>
                  <p className="mt-1 truncate text-muted-foreground">
                    {[item.paperId, item.locale, item.modelRoute].filter(Boolean).join(" / ")}
                  </p>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">{t("studio.ops.noSummaryFailures")}</p>
            )}
          </div>
        </div>
      </section>
    </div>
  )
}

function PaperVisualCompileBackfillContent({
  limit,
  force,
  triggering,
  triggerError,
  triggerSuccess,
  onLimitChange,
  onForceChange,
  onTrigger,
}: {
  limit: string
  force: boolean
  triggering: boolean
  triggerError: Error | null
  triggerSuccess: boolean
  onLimitChange: (value: string) => void
  onForceChange: (value: boolean) => void
  onTrigger: () => void
}) {
  const { t } = useI18n()
  const limitInvalid = limit.trim() ? parseOptionalPositiveInt(limit) === undefined : false
  return (
    <section className="rounded-md border border-border bg-background p-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">{t("studio.ops.readerBackfillTitle")}</h3>
          <p className="mt-1 text-xs text-muted-foreground">{t("studio.ops.readerBackfillDescription")}</p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-32 text-xs font-semibold uppercase tracking-normal text-muted-foreground">
            <span className="mb-1 block">{t("studio.ops.readerBackfillLimit")}</span>
            <Input
              type="number"
              min={1}
              value={limit}
              onChange={(event) => onLimitChange(event.target.value)}
              placeholder={t("studio.ops.readerBackfillLimitPlaceholder")}
              disabled={triggering}
            />
          </label>
          <label className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={force}
              onChange={(event) => onForceChange(event.target.checked)}
              disabled={triggering}
              className="size-4 rounded border-border"
            />
            {t("studio.ops.readerBackfillForce")}
          </label>
          <Button type="button" onClick={onTrigger} disabled={triggering || limitInvalid}>
            <RefreshCw className="size-4" />
            {triggering ? t("studio.ops.readerBackfillTriggering") : t("studio.ops.readerBackfillTrigger")}
          </Button>
        </div>
      </div>
      {limitInvalid ? <p className="mt-2 text-xs text-destructive">{t("studio.ops.readerBackfillLimitInvalid")}</p> : null}
      {triggerError ? <p className="mt-2 text-xs text-destructive">{triggerError.message}</p> : null}
      {triggerSuccess && !triggerError ? <p className="mt-2 text-xs text-success">{t("studio.ops.readerBackfillTriggered")}</p> : null}
    </section>
  )
}

function PaperIngestOpsContent({
  state,
  loading,
  error,
  triggering,
  triggerError,
  triggerSuccess,
  onTrigger,
}: {
  state?: PaperIngestOpsState
  loading: boolean
  error: Error | null
  triggering: boolean
  triggerError: Error | null
  triggerSuccess: boolean
  onTrigger: () => void
}) {
  const { t, dateTime } = useI18n()
  const latestRun = state?.runs[0]
  const repairCount = state?.repairQueue.length ?? 0
  const blockedCount = state?.blockedItems.length ?? 0
  const taxonomyCount = state?.taxonomyEvents.length ?? 0

  if (loading) {
    return (
      <div className="rounded-md border border-border bg-background p-3">
        <div className="h-24 animate-pulse rounded-md bg-secondary/50" />
      </div>
    )
  }

  if (error) {
    return (
      <StudioNotice tone="warning" title={t("studio.ops.ingestUnavailable")}>
        <p>{error.message}</p>
      </StudioNotice>
    )
  }

  return (
    <section className="rounded-md border border-border bg-background p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">{t("studio.ops.ingestTitle")}</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            {state
              ? t("studio.ops.ingestConfig", {
                  candidates: state.config.candidateLimit,
                  stars: state.config.minGithubStars,
                  confidence: Math.round(state.config.autoTaxonomyConfidence * 100),
                })
              : t("studio.ops.ingestNoState")}
          </p>
        </div>
        <Button type="button" onClick={onTrigger} disabled={triggering}>
          <Play className="size-4" />
          {triggering ? t("studio.ops.ingestTriggering") : t("studio.ops.ingestTrigger")}
        </Button>
      </div>
      {triggerError ? <p className="mt-2 text-xs text-destructive">{triggerError.message}</p> : null}
      {triggerSuccess && !triggerError ? <p className="mt-2 text-xs text-success">{t("studio.ops.ingestTriggered")}</p> : null}

      <StudioMetricGrid className="mt-4 xl:grid-cols-4 2xl:grid-cols-4">
        <StudioMetricCard
          label={t("studio.ops.ingestPublished")}
          value={latestRun?.publishedCount ?? 0}
          detail={latestRun ? `${latestRun.status} / ${dateTime(latestRun.startedAt)}` : t("studio.ops.noRuns")}
          icon={GitBranch}
          tone={(latestRun?.publishedCount ?? 0) > 0 ? "success" : "neutral"}
        />
        <StudioMetricCard
          label={t("studio.ops.ingestRepair")}
          value={repairCount}
          detail={latestRun ? t("studio.ops.ingestRepairLatest", { count: latestRun.repairQueuedCount }) : t("studio.ops.noRuns")}
          icon={Wrench}
          tone={repairCount ? "warning" : "success"}
        />
        <StudioMetricCard
          label={t("studio.ops.ingestBlocked")}
          value={blockedCount}
          detail={blockedCount ? t("studio.ops.ingestNeedsHuman") : t("studio.ops.ingestNoBlocked")}
          icon={ShieldAlert}
          tone={blockedCount ? "danger" : "success"}
        />
        <StudioMetricCard
          label={t("studio.ops.ingestTaxonomy")}
          value={taxonomyCount}
          detail={t("studio.ops.ingestAutoPublished")}
          icon={Tags}
          tone={taxonomyCount ? "info" : "neutral"}
        />
      </StudioMetricGrid>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <IngestList
          title={t("studio.ops.ingestRecentRuns")}
          empty={t("studio.ops.noRuns")}
          items={(state?.runs ?? []).slice(0, 4).map((run) => ({
            id: run.runId,
            headline: `${run.status} / ${run.publishedCount} ${t("studio.ops.ingestPublishedShort")}`,
            meta: `${dateTime(run.startedAt)} / ${run.processedCount}/${run.candidateCount}`,
          }))}
        />
        <IngestList
          title={t("studio.ops.ingestRepairQueue")}
          empty={t("studio.ops.ingestNoRepair")}
          items={(state?.repairQueue ?? []).slice(0, 4).map((item) => ({
            id: item.itemId,
            headline: `${item.step}: ${item.errorCode}`,
            meta: item.retryAt ? `${item.paperId ?? item.title ?? ""} / ${dateTime(item.retryAt)}` : item.errorMessage,
          }))}
        />
        <IngestList
          title={t("studio.ops.ingestBlockedItems")}
          empty={t("studio.ops.ingestNoBlocked")}
          items={(state?.blockedItems ?? []).slice(0, 4).map((item) => ({
            id: item.itemId,
            headline: `${item.step}: ${item.errorCode}`,
            meta: item.paperId ?? item.title ?? item.errorMessage,
          }))}
        />
      </div>
    </section>
  )
}

function IngestList({
  title,
  empty,
  items,
}: {
  title: string
  empty: string
  items: Array<{ id: string; headline: string; meta: string }>
}) {
  return (
    <div className="rounded-md border border-border bg-card p-3">
      <h4 className="text-sm font-semibold text-foreground">{title}</h4>
      <div className="mt-2 space-y-2">
        {items.length ? (
          items.map((item) => (
            <div key={item.id} className="rounded-md bg-secondary/40 px-3 py-2 text-xs">
              <p className="truncate font-semibold text-foreground">{item.headline}</p>
              <p className="mt-1 truncate text-muted-foreground">{item.meta}</p>
            </div>
          ))
        ) : (
          <p className="text-sm text-muted-foreground">{empty}</p>
        )}
      </div>
    </div>
  )
}

function SummaryRefreshForm({
  paperId,
  locale,
  reason,
  disabled,
  pending,
  error,
  success,
  onPaperIdChange,
  onLocaleChange,
  onReasonChange,
  onSubmit,
}: {
  paperId: string
  locale: Locale
  reason: string
  disabled: boolean
  pending: boolean
  error: Error | null
  success: boolean
  onPaperIdChange: (value: string) => void
  onLocaleChange: (value: Locale) => void
  onReasonChange: (value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
}) {
  const { t } = useI18n()

  return (
    <form className="rounded-md border border-border bg-background p-3" onSubmit={onSubmit}>
      <div className="mb-3 flex items-center gap-2">
        <RefreshCw className="size-4 text-link" />
        <h3 className="text-sm font-semibold text-foreground">{t("studio.ops.refreshSummary")}</h3>
      </div>
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_8rem_minmax(0,1fr)_auto] md:items-end">
        <label className="min-w-0 text-sm">
          <span className="mb-1 block text-xs font-semibold uppercase tracking-normal text-muted-foreground">{t("studio.ops.paperIdOrSlug")}</span>
          <Input value={paperId} onChange={(event) => onPaperIdChange(event.target.value)} placeholder="paper-id" disabled={pending} />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs font-semibold uppercase tracking-normal text-muted-foreground">{t("studio.ops.locale")}</span>
          <select
            className="h-9 w-full rounded-md border border-input bg-card px-3 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            value={locale}
            disabled={pending}
            onChange={(event) => onLocaleChange(event.target.value as Locale)}
          >
            <option value="en">en</option>
            <option value="zh">zh</option>
          </select>
        </label>
        <label className="min-w-0 text-sm">
          <span className="mb-1 block text-xs font-semibold uppercase tracking-normal text-muted-foreground">{t("studio.ops.reason")}</span>
          <Input value={reason} onChange={(event) => onReasonChange(event.target.value)} placeholder={t("studio.ops.reasonPlaceholder")} disabled={pending} />
        </label>
        <Button type="submit" disabled={disabled}>
          <RefreshCw className="size-4" />
          {pending ? t("studio.ops.refreshing") : t("common.refresh")}
        </Button>
      </div>
      {error ? <p className="mt-2 text-xs text-destructive">{error.message}</p> : null}
      {success && !error ? <p className="mt-2 text-xs text-success">{t("studio.ops.refreshed")}</p> : null}
    </form>
  )
}

function StatusRow({ label, status, value }: { label: string; status: string; value?: string | null }) {
  const { locale, t, dateTime } = useI18n()

  return (
    <div className="flex min-w-0 items-center justify-between gap-3 rounded-md bg-secondary/40 px-3 py-2">
      <div className="min-w-0">
        <p className="truncate font-medium text-foreground">{label}</p>
        <p className="truncate text-xs text-muted-foreground">{value ? dateTime(value) : t("studio.ops.noTimestamp")}</p>
      </div>
      <Badge variant={status === "ready" ? "success" : status === "missing" ? "muted" : "warning"}>{formatStatus(locale, status)}</Badge>
    </div>
  )
}

function opsNotices(stats: PaperReaderOpsStats, t: ReturnType<typeof useI18n>["t"]): string[] {
  const notices: string[] = []
  if (stats.dataState === "partial") {
    notices.push(t("studio.ops.notice.partial"))
  }
  if (stats.summaryCache.status === "missing") {
    notices.push(t("studio.ops.notice.summaryMissing"))
  }
  if (stats.readerCache.status === "missing") {
    notices.push(t("studio.ops.notice.readerMissing"))
  }
  if (stats.textExtraction.fileCount === 0) {
    notices.push(t("studio.ops.notice.noExtraction"))
  }
  return notices
}

function parseOptionalPositiveInt(value: string): number | undefined {
  const text = value.trim()
  if (!text) return undefined
  if (!/^[1-9]\d*$/.test(text)) return undefined
  const parsed = Number.parseInt(text, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}
