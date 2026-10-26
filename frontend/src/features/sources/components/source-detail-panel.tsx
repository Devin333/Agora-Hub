import { EmptyState } from "@/components/common/empty-state"
import { SourceHealthBadge } from "@/components/common/source-health-badge"
import { StudioField, StudioFieldGrid, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime, formatDurationMs, formatNumber, titleCase } from "@/lib/format"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { Source } from "@/types/source"

export function SourceDetailPanel({ source }: { source?: Source }) {
  const { t } = useI18n()
  if (!source) {
    return <EmptyState title={t("studio.sources.noSelected")} description={t("studio.sources.noSelectedDescription")} />
  }

  return (
    <section className="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
      <StudioPanel
        title={source.name}
        description={source.id}
        actions={<SourceHealthBadge status={source.healthStatus} />}
      >
        <div className="space-y-4">
          <StudioFieldGrid className="xl:grid-cols-2">
            <StudioField label={t("studio.sources.type")} value={titleCase(source.type)} />
            <StudioField label={t("studio.sources.enabled")} value={source.enabled ? t("common.yes") : t("common.no")} />
            <StudioField label={t("studio.sources.lastRun")} value={formatDateTime(source.lastRunAt)} />
            <StudioField label={t("studio.sources.lastSuccess")} value={formatDateTime(source.lastSuccessAt)} />
            <StudioField label={t("studio.sources.collected24h")} value={formatNumber(source.collectedCount24h)} />
            <StudioField label={t("studio.sources.avgLatency")} value={formatDurationMs(source.avgLatencyMs)} />
          </StudioFieldGrid>
          <div className="rounded-md border border-border bg-secondary/30 p-3 text-sm leading-6 text-muted-foreground">
            {source.configSummary ?? t("studio.sources.noConfigSummary")}
          </div>
        </div>
      </StudioPanel>

      <div className="space-y-4">
        <StudioPanel title={t("studio.sources.recentRunHistory")} contentClassName="space-y-2">
          {(source.recentRuns ?? []).length ? (
            (source.recentRuns ?? []).map((run) => (
              <div key={run.id} className="rounded-md border border-border bg-background p-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium text-foreground">{run.id}</span>
                  <SourceHealthBadge status={run.status} />
                </div>
                <p className="mt-1 text-muted-foreground">
                  {formatDateTime(run.startedAt)} | {t("studio.sources.items", { count: formatNumber(run.collectedCount) })} | {formatDurationMs(run.latencyMs)}
                </p>
                {run.errorMessage ? <p className="mt-1 text-destructive">{run.errorMessage}</p> : null}
              </div>
            ))
          ) : (
            <p className="text-sm text-muted-foreground">{t("studio.sources.noRunHistory")}</p>
          )}
        </StudioPanel>

        <StudioPanel title={t("studio.sources.errorsAndLatest")}>
          {source.errorSummary?.length ? (
            <ul className="mb-4 space-y-1 text-sm text-destructive">
              {source.errorSummary.map((error) => <li key={error}>{error}</li>)}
            </ul>
          ) : (
            <p className="mb-4 text-sm text-muted-foreground">{t("studio.sources.noErrorSummary")}</p>
          )}
          <div className="space-y-2">
            {(source.latestItems ?? []).map((item) => (
              <div key={item.id} className="rounded-md border border-border bg-background p-3 text-sm">
                <p className="font-medium text-foreground">{item.title}</p>
                <p className="mt-1 text-muted-foreground">{formatDateTime(item.capturedAt)}</p>
              </div>
            ))}
            {!source.latestItems?.length ? <p className="text-sm text-muted-foreground">{t("studio.sources.noLatestPreview")}</p> : null}
          </div>
        </StudioPanel>
      </div>
    </section>
  )
}
