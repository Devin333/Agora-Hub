import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioClaimEvidence } from "@/types/evidence"

export function UnsupportedClaimsPanel({ claims }: { claims: StudioClaimEvidence[] }) {
  const { locale, t } = useI18n()
  const unsupportedClaims = claims.filter((claim) => claim.status === "unsupported" || claim.status === "rejected")

  return (
    <StudioPanel title={t("studio.evidence.unsupportedRejected")}>
      {unsupportedClaims.length ? (
        <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
          {unsupportedClaims.map((claim) => (
            <article key={claim.claimId} className="px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="text-[13.5px] font-medium text-foreground">{claim.claimText}</p>
                  <p className="mt-0.5 text-label text-muted-foreground">{claim.reportSection ?? t("studio.evidence.noReportSection")}</p>
                </div>
                <Badge tone={claim.status === "rejected" ? "danger" : "info"}>{formatStatus(locale, claim.status)}</Badge>
              </div>
              {claim.failureReason ? <p className="mt-1.5 text-body2 text-muted-foreground">{claim.failureReason}</p> : null}
            </article>
          ))}
        </div>
      ) : (
        <EmptyState title={t("studio.evidence.noUnsupported")} description={t("studio.evidence.noUnsupportedDescription")} />
      )}
    </StudioPanel>
  )
}
