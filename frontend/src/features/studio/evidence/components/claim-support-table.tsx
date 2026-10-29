"use client"

import { useMemo, useState } from "react"
import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table"
import { filterClaimsByStatus } from "@/features/studio/evidence/lib/evidence-adapter"
import {
  StudioTableFrame,
  StudioTableHeader,
  studioTableCellClass
} from "@/features/studio/shared/components/studio-dashboard"
import { StudioSegmentTabs } from "@/features/studio/shared/components/studio-filters"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { ClaimSupportStatus, StudioClaimEvidence } from "@/types/evidence"

const FILTERS: Array<ClaimSupportStatus | "all"> = ["all", "accepted", "rejected", "uncertain", "unsupported"]

const STATUS_TONE: Record<ClaimSupportStatus, "success" | "danger" | "warning" | "info"> = {
  accepted: "success",
  rejected: "danger",
  uncertain: "warning",
  unsupported: "info"
}

export function ClaimSupportTable({ claims }: { claims: StudioClaimEvidence[] }) {
  const { t, status } = useI18n()
  const [statusFilter, setStatusFilter] = useState<ClaimSupportStatus | "all">("all")
  const filteredClaims = useMemo(() => filterClaimsByStatus(claims, statusFilter), [claims, statusFilter])

  if (!claims.length) {
    return <EmptyState title={t("studio.evidence.noMatrix")} description={t("studio.evidence.noMatrixDescription")} />
  }

  return (
    <section className="space-y-3">
      <StudioSegmentTabs
        className="w-fit max-w-full"
        label={t("studio.evidence.claimStatusFilters")}
        tabs={FILTERS.map((filter) => ({ id: filter, label: filter === "all" ? t("common.all") : status(filter) }))}
        active={statusFilter}
        onChange={setStatusFilter}
      />
      {!filteredClaims.length ? (
        <EmptyState title={t("studio.evidence.noMatchingClaims")} description={t("studio.evidence.noMatchingClaimsDescription")} />
      ) : (
        <StudioTableFrame>
          <Table>
            <StudioTableHeader
              columns={[
                t("studio.evidence.claim"),
                t("common.status"),
                t("studio.evidence.sources"),
                t("studio.evidence.evidence"),
                t("studio.evidence.section")
              ]}
            />
            <TableBody>
              {filteredClaims.map((claim) => (
                <TableRow key={claim.claimId}>
                  <TableCell className={`${studioTableCellClass} min-w-[280px]`}>
                    <p className="text-[13.5px] font-medium text-foreground">{claim.claimText}</p>
                    <p className="mt-0.5 font-mono text-label text-muted-foreground">{claim.claimId}</p>
                    {claim.failureReason ? <p className="mt-1.5 text-label text-muted-foreground">{claim.failureReason}</p> : null}
                  </TableCell>
                  <TableCell className={studioTableCellClass}>
                    <Badge tone={STATUS_TONE[claim.status]}>{status(claim.status)}</Badge>
                    {claim.confidence !== undefined ? <p className="mt-1.5 text-label tabular-nums text-muted-foreground">{formatConfidence(claim.confidence)} {t("studio.evidence.confidence")}</p> : null}
                  </TableCell>
                  <TableCell className={`${studioTableCellClass} min-w-[220px]`}>
                    {claim.sourceRefs.length ? (
                      <div className="space-y-1">
                        {claim.sourceRefs.slice(0, 3).map((source, index) => (
                          <p key={`${source.url ?? source.sourceId ?? source.title}-${index}`} className="break-words text-label text-muted-foreground">
                            {source.url ?? source.title ?? source.sourceId}
                          </p>
                        ))}
                      </div>
                    ) : (
                      <span className="text-label text-muted-foreground">{t("studio.evidence.noSources")}</span>
                    )}
                  </TableCell>
                  <TableCell className={`${studioTableCellClass} min-w-[180px]`}>
                    {claim.evidenceRefs.length ? (
                      <div className="space-y-1">
                        {claim.evidenceRefs.slice(0, 3).map((evidence, index) => (
                          <p key={`${evidence.evidenceId ?? evidence.quote ?? evidence.summary}-${index}`} className="break-words text-label text-muted-foreground">
                            {evidence.evidenceId ?? evidence.quote ?? evidence.summary}
                          </p>
                        ))}
                      </div>
                    ) : (
                      <span className="text-label text-muted-foreground">{t("studio.evidence.noEvidence")}</span>
                    )}
                  </TableCell>
                  <TableCell className={studioTableCellClass}>{claim.reportSection ? <span className="text-body2 text-foreground">{claim.reportSection}</span> : <span className="text-label text-muted-foreground">{t("studio.evidence.noSection")}</span>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </StudioTableFrame>
      )}
    </section>
  )
}

function formatConfidence(value: number): string {
  return `${Math.round(value <= 1 ? value * 100 : value)}%`
}
