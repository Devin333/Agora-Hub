import { AlertTriangle, CheckCircle2, CircleHelp, ShieldAlert, ShieldCheck } from "lucide-react"
import {
  StudioMetricCard,
  StudioMetricGrid,
  StudioPanel,
  type StudioIcon,
  type StudioTone
} from "@/features/studio/shared/components/studio-dashboard"
import { formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioQualityDashboard, StudioQualityStatus } from "@/types/quality"

const statusConfig: Record<StudioQualityStatus, { tone: StudioTone; icon: StudioIcon }> = {
  passed: { tone: "success", icon: ShieldCheck },
  warning: { tone: "warning", icon: AlertTriangle },
  failed: { tone: "danger", icon: ShieldAlert },
  review_required: { tone: "info", icon: CheckCircle2 },
  unknown: { tone: "neutral", icon: CircleHelp }
}

export function QualityStatusBoard({ dashboard }: { dashboard: StudioQualityDashboard }) {
  const { locale, t } = useI18n()
  return (
    <StudioMetricGrid className="xl:grid-cols-5 2xl:grid-cols-5">
      {(Object.keys(statusConfig) as StudioQualityStatus[]).map((status) => {
        const config = statusConfig[status]
        return (
          <StudioMetricCard
            key={status}
            label={formatStatus(locale, status)}
            value={dashboard.counts[status]}
            detail={t("studio.quality.reportsInGate")}
            icon={config.icon}
            tone={config.tone}
          />
        )
      })}
    </StudioMetricGrid>
  )
}

export function QualityMetricBoard({ dashboard }: { dashboard: StudioQualityDashboard }) {
  const { t } = useI18n()
  const metrics = [
    [t("studio.quality.citationCoverage"), dashboard.metrics.citationCoverage === undefined ? "n/a" : `${dashboard.metrics.citationCoverage}%`],
    [t("studio.quality.sourceFreshness"), dashboard.metrics.sourceFreshness === undefined ? "n/a" : `${dashboard.metrics.sourceFreshness}%`],
    [t("studio.quality.duplicateRate"), dashboard.metrics.duplicateRate === undefined ? "n/a" : `${dashboard.metrics.duplicateRate}%`],
    [t("studio.quality.unsupportedClaims"), String(dashboard.metrics.unsupportedClaims)]
  ]

  return (
    <StudioPanel title={t("studio.quality.gateMetrics")} description={t("studio.quality.gateMetricsDescription")}>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {metrics.map(([label, value]) => (
          <div key={label} className="rounded-xl bg-muted/60 p-3">
            <p className="text-label text-muted-foreground">{label}</p>
            <p className="mt-1.5 text-heading2 font-semibold tabular-nums text-foreground">{value}</p>
          </div>
        ))}
      </div>
    </StudioPanel>
  )
}
