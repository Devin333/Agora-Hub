"use client"

import { EmptyState } from "@/components/common/empty-state"
import { SourceHealthBadge } from "@/components/common/source-health-badge"
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table"
import {
  StudioTableFrame,
  StudioTableHeader,
  studioTableCellClass
} from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime, formatDurationMs, formatNumber, titleCase } from "@/lib/format"
import { useI18n } from "@/lib/i18n/use-i18n"
import { cn } from "@/lib/utils"
import type { Source } from "@/types/source"

export function SourceHealthTable({
  sources,
  selectedSourceId,
  onSelectSource
}: {
  sources: Source[]
  selectedSourceId?: string
  onSelectSource: (sourceId: string) => void
}) {
  const { t } = useI18n()
  if (!sources.length) {
    return <EmptyState title={t("studio.sources.noMatching")} description={t("studio.sources.noMatchingDescription")} />
  }

  if (sources.every((source) => !source.enabled)) {
    return <EmptyState title={t("studio.sources.allDisabled")} description={t("studio.sources.allDisabledDescription")} />
  }

  return (
    <StudioTableFrame>
      <Table className="min-w-[1120px]">
        <StudioTableHeader
          columns={[
            t("studio.sources.source"),
            t("studio.sources.type"),
            t("studio.sources.enabled"),
            t("studio.sources.health"),
            t("studio.sources.lastRun"),
            t("studio.sources.lastSuccess"),
            t("studio.sources.errors24h"),
            t("studio.sources.collected24h"),
            t("studio.sources.avgLatency"),
            t("studio.sources.profile")
          ]}
        />
        <TableBody className="text-body2">
          {sources.map((source) => {
            const selected = selectedSourceId === source.id
            return (
              <TableRow
                key={source.id}
                data-state={selected ? "selected" : undefined}
                aria-selected={selected}
                tabIndex={0}
                className="cursor-pointer outline-none hover:bg-accent/40 focus-visible:bg-accent/40 data-[state=selected]:bg-accent"
                onClick={() => onSelectSource(source.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault()
                    onSelectSource(source.id)
                  }
                }}
              >
                <TableCell className={studioTableCellClass}>
                  <p className="truncate font-medium text-foreground">{source.name}</p>
                  <p className="truncate text-label text-muted-foreground">{source.id}</p>
                </TableCell>
                <TableCell className={cn(studioTableCellClass, "text-muted-foreground")}>{titleCase(source.type)}</TableCell>
                <TableCell className={cn(studioTableCellClass, "text-muted-foreground")}>
                  {source.enabled ? t("studio.sources.enabledValue") : t("studio.sources.disabledValue")}
                </TableCell>
                <TableCell className={studioTableCellClass}>
                  <SourceHealthBadge status={source.healthStatus} />
                </TableCell>
                <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{formatDateTime(source.lastRunAt)}</TableCell>
                <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{formatDateTime(source.lastSuccessAt)}</TableCell>
                <TableCell
                  className={cn(
                    studioTableCellClass,
                    "tabular-nums",
                    source.errorCount24h ? "font-semibold text-warning-text" : "text-muted-foreground"
                  )}
                >
                  {formatNumber(source.errorCount24h)}
                </TableCell>
                <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{formatNumber(source.collectedCount24h)}</TableCell>
                <TableCell className={cn(studioTableCellClass, "tabular-nums text-muted-foreground")}>{formatDurationMs(source.avgLatencyMs)}</TableCell>
                <TableCell className={cn(studioTableCellClass, "max-w-48 truncate text-muted-foreground")}>
                  {source.configProfile ?? t("studio.sources.none")}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </StudioTableFrame>
  )
}
