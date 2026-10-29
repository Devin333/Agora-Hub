import type { ComponentProps } from "react"
import { CircleAlert } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StudioPanel, studioTableCellClass, studioTableHeadClass } from "@/features/studio/shared/components/studio-dashboard"
import { formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioQualityCheck, StudioQualityStatus } from "@/types/quality"

const statusVariant: Record<StudioQualityStatus, ComponentProps<typeof Badge>["variant"]> = {
  passed: "success",
  warning: "warning",
  failed: "danger",
  review_required: "info",
  unknown: "muted"
}

export function QualityCheckTable({ checks }: { checks: StudioQualityCheck[] }) {
  const { locale, t } = useI18n()
  return (
    <StudioPanel title={t("studio.quality.qualityChecks")} flush>
      <Table>
        <TableHeader className="text-label">
          <TableRow className="hover:bg-transparent">
            <TableHead className={`${studioTableHeadClass} min-w-52`}>{t("studio.quality.check")}</TableHead>
            <TableHead className={`${studioTableHeadClass} w-40`}>{t("common.status")}</TableHead>
            <TableHead className={`${studioTableHeadClass} w-28`}>{t("studio.quality.score")}</TableHead>
            <TableHead className={`${studioTableHeadClass} min-w-80`}>{t("studio.quality.message")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {checks.map((check) => (
            <TableRow key={check.id}>
              <TableCell className={studioTableCellClass}>
                <div className="flex min-w-0 items-center gap-2">
                  {check.userActionRequired ? <CircleAlert size={16} strokeWidth={1.8} aria-hidden="true" className="shrink-0 text-warning" /> : null}
                  <span className="truncate text-[13.5px] font-medium text-foreground">{check.name}</span>
                </div>
              </TableCell>
              <TableCell className={studioTableCellClass}>
                <Badge variant={statusVariant[check.status]}>{formatQualityStatus(locale, check.status)}</Badge>
              </TableCell>
              <TableCell className={`${studioTableCellClass} tabular-nums text-muted-foreground`}>{check.score === undefined ? "n/a" : check.score}</TableCell>
              <TableCell className={`${studioTableCellClass} max-w-[34rem] text-muted-foreground`}>
                <span className="line-clamp-2">{check.message ?? t("studio.quality.noMessage")}</span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </StudioPanel>
  )
}

export function QualityStatusBadge({ status }: { status: StudioQualityStatus }) {
  const { locale } = useI18n()
  return <Badge variant={statusVariant[status]}>{formatQualityStatus(locale, status)}</Badge>
}

function formatQualityStatus(locale: "zh" | "en", status: StudioQualityStatus): string {
  return formatStatus(locale, status)
}
