import { AlertTriangle, Archive, CheckCircle2, FileJson2, ListTree, ShieldCheck, Workflow } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ArtifactPreview } from "@/features/studio/artifacts/components/artifact-preview"
import { ReplayReadinessBadge } from "@/features/studio/artifacts/components/artifact-status-badge"
import { StudioCodeBlock, StudioMetricCard, StudioMetricGrid, StudioNotice, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioReplayBundle } from "@/types/artifact"

export function ReplayBundleViewer({ replay }: { replay: StudioReplayBundle }) {
  const { t } = useI18n()
  const integrityValid = replay.integrity.valid === true

  return (
    <div className="space-y-5">
      <StudioMetricGrid className="xl:grid-cols-5 2xl:grid-cols-5">
        <StudioMetricCard label={t("studio.artifacts.readiness")} value={<ReplayReadinessBadge replay={replay} />} detail={replay.ready ? t("studio.artifacts.replayReady") : t("studio.artifacts.reviewRequired")} icon={ShieldCheck} tone={replay.ready ? "success" : "warning"} />
        <StudioMetricCard label={t("studio.artifacts.events")} value={String(replay.eventCount)} detail={t("studio.artifacts.replayEvents")} icon={Workflow} />
        <StudioMetricCard label={t("studio.quality.artifacts")} value={String(replay.artifactCount)} detail={t("studio.artifacts.replayArtifacts")} icon={Archive} />
        <StudioMetricCard label={t("studio.artifacts.stepResults")} value={String(replay.stepResultCount)} detail={t("studio.artifacts.stepPayloads")} icon={ListTree} />
        <StudioMetricCard label={t("studio.artifacts.integrity")} value={<Badge variant={integrityValid ? "success" : "warning"}>{integrityValid ? t("studio.artifacts.valid") : t("studio.artifacts.check")}</Badge>} detail={t("studio.artifacts.bundleValidation")} icon={integrityValid ? CheckCircle2 : AlertTriangle} tone={integrityValid ? "success" : "warning"} />
      </StudioMetricGrid>

      {replay.eventsError ? (
        <StudioNotice tone="danger" title={t("studio.artifacts.replayEventsError")}>
          {replay.eventsError}
        </StudioNotice>
      ) : null}

      <Tabs defaultValue="manifest">
        <TabsList className="h-auto max-w-full flex-wrap justify-start">
          <TabsTrigger value="manifest">{t("studio.artifacts.manifest")}</TabsTrigger>
          <TabsTrigger value="events">{t("studio.artifacts.events")}</TabsTrigger>
          <TabsTrigger value="artifacts">{t("studio.quality.artifacts")}</TabsTrigger>
          <TabsTrigger value="steps">{t("studio.artifacts.stepResults")}</TabsTrigger>
          <TabsTrigger value="integrity">{t("studio.artifacts.integrity")}</TabsTrigger>
        </TabsList>
        <TabsContent value="manifest">
          <JsonPanel title="manifest.json" value={replay.manifest} />
        </TabsContent>
        <TabsContent value="events">
          <JsonPanel title="events" value={replay.events} />
        </TabsContent>
        <TabsContent value="artifacts">
          <div className="space-y-3">
            {replay.artifacts.map((artifact) => (
              <ArtifactPreview key={artifact.artifactKey} artifact={artifact} />
            ))}
          </div>
        </TabsContent>
        <TabsContent value="steps">
          <JsonPanel title="step_results" value={replay.stepResults} />
        </TabsContent>
        <TabsContent value="integrity">
          <JsonPanel title="integrity" value={replay.integrity} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function JsonPanel({ title, value }: { title: string; value: unknown }) {
  const isIntegrity = title === "integrity" && typeof value === "object" && value && "valid" in value
  const valid = isIntegrity && (value as { valid?: unknown }).valid === true
  return (
    <StudioPanel
      title={title}
      actions={isIntegrity ? (valid ? <CheckCircle2 size={16} strokeWidth={1.8} aria-hidden="true" className="text-success" /> : <AlertTriangle size={16} strokeWidth={1.8} aria-hidden="true" className="text-warning" />) : <FileJson2 size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />}
    >
      <StudioCodeBlock className="max-h-[36rem] leading-6">{JSON.stringify(value, null, 2)}</StudioCodeBlock>
    </StudioPanel>
  )
}
