import { EmptyState } from "@/components/common/empty-state";
import { Badge } from "@/components/ui/badge";
import { StudioCodeBlock, StudioField, StudioFieldGrid, StudioPanel } from "@/features/studio/shared/components/studio-dashboard";
import { formatBytes, formatDateTime, titleCase } from "@/lib/format";
import { useI18n } from "@/lib/i18n/use-i18n";
import type { Artifact } from "@/types/artifact";

export function ArtifactPreviewPanel({ artifact }: { artifact?: Artifact }) {
  const { t } = useI18n();
  if (!artifact) {
    return <EmptyState title={t("studio.artifacts.notSelected")} description={t("studio.artifacts.notSelectedDescription")} />;
  }

  return (
    <StudioPanel
      title={artifact.filename}
      description={artifact.id}
      actions={<Badge variant="info">{titleCase(artifact.artifactType)}</Badge>}
      contentClassName="space-y-3"
    >
      <StudioFieldGrid className="lg:grid-cols-4 xl:grid-cols-4">
        <StudioField label={t("studio.artifacts.run")} value={artifact.runId ?? t("common.none")} />
        <StudioField label={t("studio.artifacts.step")} value={artifact.stepId ?? t("common.none")} />
        <StudioField label={t("studio.artifacts.size")} value={formatBytes(artifact.sizeBytes)} />
        <StudioField label={t("studio.artifacts.createdAt")} value={formatDateTime(artifact.createdAt)} />
      </StudioFieldGrid>
      {artifact.preview ? (
        <StudioCodeBlock className="max-h-96 leading-6 text-muted-foreground">{artifact.preview}</StudioCodeBlock>
      ) : (
        <p className="py-6 text-center text-body2 text-muted-foreground">{t("studio.artifacts.noPreview")}</p>
      )}
    </StudioPanel>
  );
}
