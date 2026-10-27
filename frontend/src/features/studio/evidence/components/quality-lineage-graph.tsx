import { Badge } from "@/components/common/badge"
import { StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioClaimEvidence } from "@/types/evidence"

const STATUS_TONE = {
  accepted: "success",
  rejected: "danger",
  uncertain: "warning",
  unsupported: "info"
} as const

const LINEAGE_GRID = "grid gap-2 md:grid-cols-5"

/** Source → evidence → claim → section → decision lineage, rendered as a rakazo divided list. */
export function QualityLineageGraph({ claims }: { claims: StudioClaimEvidence[] }) {
  const { locale, t } = useI18n()
  const featuredClaims = claims.slice(0, 5)

  return (
    <StudioPanel title={t("studio.evidence.lineage")}>
      <div className="overflow-hidden rounded-xl border border-border">
        <div className={`${LINEAGE_GRID} border-b border-border bg-muted/50 px-4 py-2 text-label font-medium text-muted-foreground`}>
          <span>{t("studio.sources.source")}</span>
          <span>{t("studio.evidence.evidence")}</span>
          <span>{t("studio.evidence.claim")}</span>
          <span>{t("studio.evidence.reportSection")}</span>
          <span>{t("studio.evidence.qualityDecision")}</span>
        </div>
        {featuredClaims.length ? (
          <div className="divide-y divide-border">
            {featuredClaims.map((claim) => (
              <div key={claim.claimId} className={`${LINEAGE_GRID} px-4 py-3 text-body2`}>
                <LineageCell value={claim.sourceRefs[0]?.url ?? claim.sourceRefs[0]?.title ?? claim.sourceRefs[0]?.sourceId ?? t("studio.evidence.noSource")} />
                <LineageCell value={claim.evidenceRefs[0]?.evidenceId ?? claim.evidenceRefs[0]?.summary ?? claim.evidenceRefs[0]?.quote ?? t("studio.evidence.noEvidence")} />
                <LineageCell value={claim.claimText} strong />
                <LineageCell value={claim.reportSection ?? t("studio.evidence.noSection")} />
                <div className="min-w-0">
                  <Badge tone={STATUS_TONE[claim.status]}>{formatStatus(locale, claim.status)}</Badge>
                  {claim.failureReason ? <p className="mt-1.5 text-label text-muted-foreground">{claim.failureReason}</p> : null}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="px-4 py-6 text-center text-body2 text-muted-foreground">{t("studio.evidence.noClaimLineage")}</p>
        )}
      </div>
    </StudioPanel>
  )
}

function LineageCell({ value, strong = false }: { value: string; strong?: boolean }) {
  return <p className={`min-w-0 break-words ${strong ? "text-[13.5px] font-medium text-foreground" : "text-muted-foreground"}`}>{value}</p>
}
