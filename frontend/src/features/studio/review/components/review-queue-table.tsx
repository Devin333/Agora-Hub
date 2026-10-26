"use client"

import Link from "next/link"
import { ExternalLink } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Badge, type BadgeProps } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ReviewRiskBadge } from "@/features/studio/review/components/review-risk-badge"
import { StudioTableFrame } from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime as formatLocalizedDateTime, formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioReviewItem } from "@/types/review"

const statusVariant: Record<StudioReviewItem["status"], BadgeProps["variant"]> = {
  pending: "info",
  approved: "success",
  rejected: "danger",
  expired: "muted"
}

export function ReviewQueueTable({ items }: { items: StudioReviewItem[] }) {
  const { locale, t } = useI18n()
  if (!items.length) {
    return <EmptyState title={t("studio.review.noItems")} description={t("studio.review.noItemsDescription")} />
  }

  return (
    <StudioTableFrame>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="min-w-52">{t("studio.review.approvalId")}</TableHead>
              <TableHead className="min-w-44">{t("studio.review.requestedAction")}</TableHead>
              <TableHead className="min-w-28">{t("studio.review.risk")}</TableHead>
              <TableHead className="min-w-28">{t("common.status")}</TableHead>
              <TableHead className="min-w-40">{t("studio.review.runId")}</TableHead>
              <TableHead className="min-w-40">{t("studio.review.reportId")}</TableHead>
              <TableHead className="min-w-36">{t("studio.review.requestedBy")}</TableHead>
              <TableHead className="min-w-40">{t("studio.review.requestedAt")}</TableHead>
              <TableHead className="min-w-40">{t("studio.review.expiresAt")}</TableHead>
              <TableHead className="min-w-64">{t("studio.review.reason")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow key={item.approvalId} className={item.riskLevel === "critical" ? "bg-destructive/5" : undefined}>
                <TableCell className="font-medium">
                  <Link className="inline-flex max-w-52 items-center gap-2 truncate text-primary hover:underline" href={`/studio/review/${encodeURIComponent(item.approvalId)}`}>
                    <span className="truncate">{item.approvalId}</span>
                    <ExternalLink className="size-3.5 shrink-0" />
                  </Link>
                </TableCell>
                <TableCell className="max-w-48 truncate">{item.requestedAction}</TableCell>
                <TableCell>
                  <ReviewRiskBadge riskLevel={item.riskLevel} />
                </TableCell>
                <TableCell>
                  <Badge variant={statusVariant[item.status]}>{formatStatus(locale, item.status)}</Badge>
                </TableCell>
                <TableCell>{item.runId ? <ResourceLink href={`/studio/runs/${encodeURIComponent(item.runId)}`} label={item.runId} /> : <MutedDash />}</TableCell>
                <TableCell>{item.reportId ? <ResourceLink href={`/reports/${encodeURIComponent(item.reportId)}`} label={item.reportId} /> : <MutedDash />}</TableCell>
                <TableCell className="max-w-36 truncate">{item.requestedBy ?? <MutedDash />}</TableCell>
                <TableCell className="whitespace-nowrap text-muted-foreground">{formatReviewDateTime(locale, item.requestedAt)}</TableCell>
                <TableCell className="whitespace-nowrap text-muted-foreground">{formatReviewDateTime(locale, item.expiresAt)}</TableCell>
                <TableCell className="max-w-72 truncate text-muted-foreground">{item.reason ?? <MutedDash />}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </StudioTableFrame>
  )
}

function ResourceLink({ href, label }: { href: string; label: string }) {
  return (
    <Link className="inline-flex max-w-40 items-center gap-1 truncate text-primary hover:underline" href={href}>
      <span className="truncate">{label}</span>
      <ExternalLink className="size-3 shrink-0" />
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
