import { Archive, Clock, FileJson2, ListTree, Workflow } from "lucide-react"
import { StudioMetricCard, StudioMetricGrid } from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime, formatNumber } from "@/lib/format"
import { formatDataState, formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioArtifactRunSummary } from "@/types/artifact"

export function ArtifactRunSummaryGrid({ run }: { run: StudioArtifactRunSummary }) {
  const { locale, t } = useI18n()
  return (
    <StudioMetricGrid className="xl:grid-cols-5 2xl:grid-cols-5">
      <StudioMetricCard label={t("studio.quality.artifacts")} value={formatNumber(run.artifactCount)} detail={formatDataState(locale, run.artifactStatus)} icon={Archive} tone={run.artifactStatus === "ready" ? "success" : "warning"} />
      <StudioMetricCard label={t("studio.artifacts.events")} value={formatNumber(run.eventCount)} detail={t("studio.artifacts.runtimeEvents")} icon={Workflow} />
      <StudioMetricCard label={t("studio.artifacts.stepResults")} value={formatNumber(run.stepResultCount)} detail={t("studio.artifacts.stepOutputRecords")} icon={ListTree} />
      <StudioMetricCard label={t("common.status")} value={formatStatus(locale, run.status)} detail={run.profile ?? t("studio.artifacts.unknownProfile")} icon={FileJson2} />
      <StudioMetricCard label={t("studio.runs.started")} value={<span className="text-body1 font-medium">{formatDateTime(run.startedAt)}</span>} detail={t("studio.artifacts.runStartTime")} icon={Clock} />
    </StudioMetricGrid>
  )
}
