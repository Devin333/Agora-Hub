import { EmptyState } from "@/components/common/empty-state"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StudioPanel, studioTableCellClass, studioTableHeadClass } from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime } from "@/lib/format"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioLineageRef } from "@/types/artifact"

export function LineageViewer({ lineage }: { lineage: StudioLineageRef[] }) {
  const { t } = useI18n()
  return (
    <StudioPanel title={t("studio.artifacts.lineage")} description={t("studio.artifacts.lineageDescription")} flush>
      {!lineage.length ? (
        <EmptyState title={t("studio.artifacts.noLineage")} description={t("studio.artifacts.noLineageDescription")} />
      ) : (
        <Table className="text-body2">
            <TableHeader className="text-label">
              <TableRow className="hover:bg-transparent">
                <TableHead className={studioTableHeadClass}>{t("studio.artifacts.direction")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.sources.source")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.artifacts.target")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.runs.runId")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.artifacts.relation")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.artifacts.created")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {lineage.map((item) => (
                <TableRow key={item.lineageId ?? `${item.sourceType}:${item.sourceId}:${item.targetId}`}>
                  <TableCell className={studioTableCellClass}>
                    <Badge variant="info">{item.direction}</Badge>
                  </TableCell>
                  <TableCell className={`${studioTableCellClass} max-w-[18rem]`}>
                    <p className="truncate text-[13.5px] font-medium text-foreground">{item.sourceType}</p>
                    <p className="mt-0.5 truncate text-label text-muted-foreground">{item.sourceId}</p>
                  </TableCell>
                  <TableCell className={`${studioTableCellClass} max-w-[18rem]`}>
                    <p className="truncate text-[13.5px] font-medium text-foreground">{item.targetType}</p>
                    <p className="mt-0.5 truncate text-label text-muted-foreground">{item.targetId}</p>
                  </TableCell>
                  <TableCell className={`${studioTableCellClass} max-w-[14rem] truncate font-mono text-label`}>{item.runId}</TableCell>
                  <TableCell className={studioTableCellClass}>{item.relationType ?? "derived_from"}</TableCell>
                  <TableCell className={studioTableCellClass}>{formatDateTime(item.createdAt)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
        </Table>
      )}
    </StudioPanel>
  )
}
