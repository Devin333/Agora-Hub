import { EmptyState } from "@/components/common/empty-state"
import { StudioCodeBlock, StudioField, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioLlmTrace } from "@/types/evidence"

export function LlmTracePanel({ trace }: { trace?: StudioLlmTrace }) {
  const { t } = useI18n()
  if (!trace) {
    return <EmptyState title={t("studio.evidence.noLlmTrace")} description={t("studio.evidence.noLlmTraceDescription")} />
  }

  const metrics: Array<[string, string | number]> = [
    [t("studio.evidence.deployment"), trace.selectedDeploymentId ?? t("common.unknown")],
    [t("studio.evidence.fallback"), trace.fallbackUsed ? t("common.yes") : t("common.no")],
    [t("studio.evidence.fallbackCount"), trace.fallbackCount ?? 0],
    [t("studio.evidence.providerErrors"), trace.providerErrorCount ?? 0],
    [t("studio.evidence.cooldownSkips"), trace.cooldownSkipCount ?? 0],
    [t("studio.evidence.routerEvents"), trace.routerEventCount ?? 0]
  ]

  return (
    <StudioPanel title={t("studio.evidence.llmTrace")} contentClassName="space-y-3">
      <div className="grid gap-2 sm:grid-cols-2">
        {metrics.map(([label, value]) => (
          <StudioField key={label} label={label} value={<span className="block break-words tabular-nums">{String(value)}</span>} />
        ))}
      </div>
      <StudioCodeBlock className="max-h-80">{JSON.stringify(trace.sanitized, null, 2)}</StudioCodeBlock>
    </StudioPanel>
  )
}
