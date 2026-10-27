"use client"

import Link from "next/link"
import { ExternalLink } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Badge, type BadgeProps } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ReviewRiskBadge } from "@/features/studio/review/components/review-risk-badge"
import { StudioTableFrame, studioTableCellClass, studioTableHeadClass } from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime as formatLocalizedDateTime, formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioReviewItem } from "@/types/review"

const statusVariant: Record<StudioReviewItem["status"], BadgeProps["variant"]> = {
  pending: "info",
  approved: "success",
  rejected: "danger",
  expired: "muted"
}

const linkFocusClass = "rounded-md outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"

export function ReviewQueueTable({ items }: { items: StudioReviewItem[] }) {
  const { locale, t } = useI18n()
  if (!items.length) {
    return <EmptyState title={t("studio.review.noItems")} description={t("studio.review.noItemsDescription")} />
  }

  const columns: Array<[string, string]> = [
    [t("studio.review.approvalId"), "min-w-52"],
    [t("studio.review.requestedAction"), "min-w-44"],
    [t("studio.review.risk"), "min-w-28"],
    [t("common.status"), "min-w-28"],
    [t("studio.review.runId"), "min-w-40"],
    [t("studio.review.reportId"), "min-w-40"],
    [t("studio.review.requestedBy"), "min-w-36"],
    [t("studio.review.requestedAt"), "min-w-40"],
    [t("studio.review.expiresAt"), "min-w-40"],
    [t("studio.review.reason"), "min-w-64"]
  ]

  return (
    <StudioTableFrame>
      <Table>
        <TableHeader className="text-label">
          <TableRow className="hover:bg-transparent">
            {columns.map(([label, width]) => (
              <TableHead key={label} className={`${studioTableHeadClass} ${width}`}>
                {label}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody className="text-body2">
          {items.map((item) => (
            <TableRow key={item.approvalId} className={item.riskLevel === "critical" ? "bg-destructive/5" : undefined}>
              <TableCell className={studioTableCellClass}>
                <Link
                  className={`${linkFocusClass} inline-flex max-w-52 items-center gap-1.5 truncate font-mono text-label font-medium text-link`}
                  href={`/studio/review/${encodeURIComponent(item.approvalId)}`}
                >
                  <span className="truncate">{item.approvalId}</span>
                  <ExternalLink size={13} strokeWidth={1.8} aria-hidden="true" className="shrink-0" />
                </Link>
              </TableCell>
              <TableCell className={`${studioTableCellClass} max-w-48 truncate font-medium text-foreground`}>{item.requestedAction}</TableCell>
              <TableCell className={studioTableCellClass}>
                <ReviewRiskBadge riskLevel={item.riskLevel} />
              </TableCell>
              <TableCell className={studioTableCellClass}>
                <Badge variant={statusVariant[item.status]}>{formatStatus(locale, item.status)}</Badge>
              </TableCell>
              <TableCell className={studioTableCellClass}>
                {item.runId ? <ResourceLink href={`/studio/runs/${encodeURIComponent(item.runId)}`} label={item.runId} /> : <MutedDash />}
              </TableCell>
              <TableCell className={studioTableCellClass}>
                {item.reportId ? <ResourceLink href={`/reports/${encodeURIComponent(item.reportId)}`} label={item.reportId} /> : <MutedDash />}
              </TableCell>
              <TableCell className={`${studioTableCellClass} max-w-36 truncate`}>{item.requestedBy ?? <MutedDash />}</TableCell>
              <TableCell className={`${studioTableCellClass} whitespace-nowrap tabular-nums text-muted-foreground`}>{formatReviewDateTime(locale, item.requestedAt)}</TableCell>
              <TableCell className={`${studioTableCellClass} whitespace-nowrap tabular-nums text-muted-foreground`}>{formatReviewDateTime(locale, item.expiresAt)}</TableCell>
              <TableCell className={`${studioTableCellClass} max-w-72 truncate text-muted-foreground`}>{item.reason ?? <MutedDash />}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </StudioTableFrame>
  )
}

function ResourceLink({ href, label }: { href: string; label: string }) {
  return (
    <Link className={`${linkFocusClass} inline-flex max-w-40 items-center gap-1 truncate font-mono text-label text-link`} href={href}>
      <span className="truncate">{label}</span>
      <ExternalLink size={12} strokeWidth={1.8} aria-hidden="true" className="shrink-0" />
    </Link>
  )
}

function MutedDash() {
  return <span className="text-muted-foreground">-</span>
}

function formatReviewDateTime(locale: "zh" | "en", value: string | undefined): string {
  if (!value) return "-"
  return formatLocalizedDateTime(locale, value)
}
