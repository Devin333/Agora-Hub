"use client"

import { SourceDetailPanel } from "@/features/sources/components/source-detail-panel"
import { SourceHealthTable } from "@/features/sources/components/source-health-table"
import { SourceMetrics } from "@/features/sources/components/source-metrics"
import { SourceToolbar } from "@/features/sources/components/source-toolbar"
import { useSources } from "@/features/sources/hooks/use-sources"
import { StudioNotice, StudioPageHeader } from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"

export function SourcesPageClient() {
  const { t } = useI18n()
  const {
    allSources,
    sources,
    filters,
    setFilters,
    selectedSource,
    setSelectedSourceId,
    isLoading,
    isFetchingPreview,
    error,
    isUsingMockFallback
  } = useSources()

  return (
    <div className="space-y-5">
      <StudioPageHeader
        eyebrow={t("studio.nav.system")}
        title={t("studio.module.sources.title")}
        description={t("studio.module.sources.description")}
      />

      {isLoading ? (
        <StudioNotice tone="info" title={t("studio.sources.loadingTitle")}>
          {t("studio.sources.loadingDescription")}
        </StudioNotice>
      ) : null}
      {error ? (
        <StudioNotice tone="warning" title={t("studio.sources.fallbackTitle")}>
          {t("studio.sources.fallbackDescription", {
            message: error instanceof Error ? error.message : t("studio.sources.requestFailed")
          })}
        </StudioNotice>
      ) : null}
      {!error && !isUsingMockFallback ? (
        <StudioNotice tone="success" title={t("studio.sources.connectedTitle")}>
          {t("studio.sources.connectedDescription", { count: allSources.length })}
          {isFetchingPreview ? t("studio.sources.fetchingPreview") : ""}
        </StudioNotice>
      ) : null}

      <SourceMetrics sources={allSources} />
      <SourceToolbar filters={filters} onChange={setFilters} />
      <SourceHealthTable sources={sources} selectedSourceId={selectedSource?.id} onSelectSource={setSelectedSourceId} />
      <SourceDetailPanel source={selectedSource} />
    </div>
  )
}
