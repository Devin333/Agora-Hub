import { Badge } from "@/components/ui/badge";
import { formatBytes, formatDateTime, titleCase } from "@/lib/format";
import { useI18n } from "@/lib/i18n/use-i18n";
import { cn } from "@/lib/utils";
import type { Artifact } from "@/types/artifact";

/** Rakazo artifact grid card: hairline `rounded-2xl` surface with a hover tint. */
export function ArtifactCard({ artifact, selected, onSelect }: { artifact: Artifact; selected?: boolean; onSelect?: () => void }) {
  const { t } = useI18n();
  return (
    <button
      type="button"
      aria-pressed={selected}
      className={cn(
        "block w-full space-y-3 rounded-2xl border border-border p-4 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
        selected ? "bg-accent" : "bg-card hover:bg-accent/40"
      )}
      onClick={onSelect}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-[13.5px] font-medium text-foreground">{artifact.filename}</p>
          <p className="mt-0.5 truncate font-mono text-label text-muted-foreground">{artifact.id}</p>
        </div>
        <Badge variant="info">{titleCase(artifact.artifactType)}</Badge>
      </div>
      <div className="grid gap-1.5 text-label text-muted-foreground sm:grid-cols-2">
        <span className="truncate">{artifact.runId ?? t("studio.artifacts.noRun")}</span>
        <span className="tabular-nums">{formatBytes(artifact.sizeBytes)}</span>
        <span className="truncate">{artifact.stepId ?? t("studio.artifacts.noStep")}</span>
        <span>{formatDateTime(artifact.createdAt)}</span>
      </div>
    </button>
  );
}
