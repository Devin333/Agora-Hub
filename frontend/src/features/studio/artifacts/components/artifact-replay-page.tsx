"use client"

import Link from "next/link"
import { Archive, Eye, RotateCcw } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Button } from "@/components/ui/button"
import { ArtifactNotice } from "@/features/studio/artifacts/components/artifact-notice"
import { ArtifactPreview } from "@/features/studio/artifacts/components/artifact-preview"
import { ArtifactRunSummaryGrid } from "@/features/studio/artifacts/components/artifact-run-summary-grid"
import { ArtifactListPanel } from "@/features/studio/artifacts/components/artifact-list-panel"
import { ArtifactStatusBadge } from "@/features/studio/artifacts/components/artifact-status-badge"
import { LineageViewer } from "@/features/studio/artifacts/components/lineage-viewer"
import { ReplayBundleViewer } from "@/features/studio/artifacts/components/replay-bundle-viewer"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import {
  StudioField,
  StudioFieldGrid,
  StudioPageHeader,
  StudioPanel,
  studioTableCellClass,
  studioTableHeadClass
} from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime, formatNumber } from "@/lib/format"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioArtifactRunDetail, StudioArtifactRunSummary, StudioLineageRef, StudioReplayBundle } from "@/types/artifact"

export function ArtifactReplayHomePage({ runs, notices }: { runs: StudioArtifactRunSummary[]; notices: string[] }) {
  const { locale, t } = useI18n()
  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={locale === "zh" ? "运行时" : "Runtime"}
        title={t("studio.module.artifactReplay.title")}
        description={t("studio.module.artifactReplay.description")}
      />
      <ArtifactNotice notices={notices} />

      <StudioPanel title={t("studio.artifacts.recentRuns")} description={t("studio.artifacts.recentRunsDescription")} flush>
        {!runs.length ? (
          <EmptyState title={t("studio.artifacts.noRuns")} description={t("studio.artifacts.noRunsDescription")} />
        ) : (
          <Table className="min-w-[1080px] text-body2">
            <TableHeader className="text-label">
              <TableRow className="hover:bg-transparent">
                <TableHead className={studioTableHeadClass}>{t("studio.runs.runId")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("common.status")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.quality.artifacts")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.artifacts.events")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.artifacts.stepResults")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.artifacts.manifest")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("studio.runs.started")}</TableHead>
                <TableHead className={studioTableHeadClass}>{t("common.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.runId} className="hover:bg-accent/40">
                  <TableCell className={`${studioTableCellClass} max-w-[16rem]`}>
                    <p className="truncate text-[13.5px] font-medium text-foreground">{run.runId}</p>
                    <p className="mt-0.5 truncate text-label text-muted-foreground">{run.workflowId ?? t("studio.artifacts.unknownWorkflow")}</p>
                  </TableCell>
                  <TableCell className={studioTableCellClass}>
                    <div className="flex flex-wrap items-center gap-2">
                      <ArtifactStatusBadge status={run.artifactStatus} />
                      <span className="text-label text-muted-foreground">{run.status}</span>
                    </div>
                  </TableCell>
                  <TableCell className={`${studioTableCellClass} tabular-nums`}>{formatNumber(run.artifactCount)}</TableCell>
                  <TableCell className={`${studioTableCellClass} tabular-nums`}>{formatNumber(run.eventCount)}</TableCell>
                  <TableCell className={`${studioTableCellClass} tabular-nums`}>{formatNumber(run.stepResultCount)}</TableCell>
                  <TableCell className={`${studioTableCellClass} max-w-[18rem] truncate font-mono text-label text-muted-foreground`}>{run.manifestPath ?? t("studio.artifacts.noManifestPath")}</TableCell>
                  <TableCell className={`${studioTableCellClass} text-muted-foreground`}>{formatDateTime(run.startedAt)}</TableCell>
                  <TableCell className={studioTableCellClass}>
                    <div className="flex flex-wrap gap-1.5">
                      <Button asChild variant="outline" size="sm">
                        <Link href={`/studio/artifacts/runs/${encodeURIComponent(run.runId)}`}>
                          <Eye size={15} strokeWidth={1.8} aria-hidden="true" />
                          {t("studio.artifacts.view")}
                        </Link>
                      </Button>
                      <Button asChild variant="ghost" size="sm">
                        <Link href={`/studio/artifacts/runs/${encodeURIComponent(run.runId)}/replay`}>
                          <RotateCcw size={15} strokeWidth={1.8} aria-hidden="true" />
                          {t("studio.artifacts.replay")}
                        </Link>
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </StudioPanel>
    </main>
  )
}

export function ArtifactRunDetailPage({
  detail,
  selectedArtifactKey
}: {
  detail: StudioArtifactRunDetail
  selectedArtifactKey?: string
}) {
  const { t } = useI18n()
  const selectedArtifact =
    detail.artifacts.find((artifact) => artifact.artifactKey === selectedArtifactKey) ?? detail.selectedArtifact

  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={t("studio.module.artifactReplay.title")}
        title={t("studio.artifacts.runArtifacts")}
        description={t("studio.artifacts.runArtifactsDescription")}
        actions={
          <Button asChild variant="outline">
            <Link href={`/studio/artifacts/runs/${encodeURIComponent(detail.run.runId)}/replay`}>
              <RotateCcw size={16} strokeWidth={1.8} aria-hidden="true" />
              {t("studio.artifacts.replayBundle")}
            </Link>
          </Button>
        }
        meta={<ArtifactStatusBadge status={detail.run.artifactStatus} />}
      />
      <ArtifactNotice notices={[...detail.run.notices, ...detail.notices]} />
      <ArtifactRunSummaryGrid run={detail.run} />

      <StudioPanel title={t("studio.artifacts.runMetadata")} description={detail.run.runId} actions={<ArtifactStatusBadge status={detail.run.artifactStatus} />}>
        <StudioFieldGrid>
          <StudioField label={t("studio.artifacts.workflow")} value={detail.run.workflowId ?? t("common.unknown")} />
          <StudioField label={t("studio.artifacts.profile")} value={detail.run.profile ?? t("common.unknown")} />
          <StudioField label={t("studio.artifacts.manifest")} value={detail.run.manifestPath ?? t("studio.artifacts.noManifestPath")} />
          <StudioField label={t("studio.artifacts.finished")} value={formatDateTime(detail.run.finishedAt)} />
        </StudioFieldGrid>
      </StudioPanel>

      <section className="grid gap-3 xl:grid-cols-[minmax(18rem,0.9fr)_minmax(0,1.4fr)]">
        <ArtifactListPanel runId={detail.run.runId} artifacts={detail.artifacts} selectedArtifactKey={selectedArtifact?.artifactKey} />
        <ArtifactPreview artifact={selectedArtifact} />
      </section>

      {detail.replay ? (
        <ReplayDigest replay={detail.replay} />
      ) : (
        <EmptyState title={t("studio.artifacts.noReplayDigest")} description={t("studio.artifacts.noReplayDigestDescription")} />
      )}
      <LineageViewer lineage={detail.lineage} />
    </main>
  )
}

export function ArtifactRunReplayPage({
  replay,
  lineage
}: {
  replay: StudioReplayBundle
  lineage: StudioLineageRef[]
}) {
  const { t } = useI18n()
  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={t("studio.module.artifactReplay.title")}
        title={t("studio.artifacts.replayBundle")}
        description={t("studio.artifacts.replayBundleDescription")}
        actions={
          <Button asChild variant="outline">
            <Link href={`/studio/artifacts/runs/${encodeURIComponent(replay.runId)}`}>
              <Archive size={16} strokeWidth={1.8} aria-hidden="true" />
              {t("studio.artifacts.backToArtifacts")}
            </Link>
          </Button>
        }
      />
      <ArtifactNotice notices={replay.notices} />
      <ReplayBundleViewer replay={replay} />
      <LineageViewer lineage={lineage} />
    </main>
  )
}

function ReplayDigest({ replay }: { replay: StudioReplayBundle }) {
  const { t } = useI18n()
  return (
    <StudioPanel
      title={t("studio.artifacts.replayDigest")}
      description={replay.manifestPath ?? t("studio.artifacts.noManifestPath")}
      actions={
        <Button asChild variant="ghost" size="sm">
          <Link href={`/studio/artifacts/runs/${encodeURIComponent(replay.runId)}/replay`}>
            <RotateCcw size={16} strokeWidth={1.8} aria-hidden="true" />
            {t("studio.artifacts.openReplay")}
          </Link>
        </Button>
      }
    >
      <StudioFieldGrid>
        <StudioField label={t("studio.artifacts.events")} value={formatNumber(replay.eventCount)} />
        <StudioField label={t("studio.quality.artifacts")} value={formatNumber(replay.artifactCount)} />
        <StudioField label={t("studio.artifacts.stepResults")} value={formatNumber(replay.stepResultCount)} />
        <StudioField label={t("studio.artifacts.readiness")} value={replay.readinessLabel} />
      </StudioFieldGrid>
    </StudioPanel>
  )
}
