import { FileJson, FileText } from "lucide-react"
import { Badge } from "@/components/common/badge"
import { formatBytes } from "@/features/studio/runs/lib/run-format"
import { formatDateTime, titleCase } from "@/lib/format"
import type { Artifact } from "@/types/agent"

export function ArtifactCard({ artifact, selected, onSelect }: { artifact: Artifact; selected: boolean; onSelect: () => void }) {
  const Icon = artifact.artifactType === "markdown" || artifact.artifactType === "report" ? FileText : FileJson

  return (
    <button
      type="button"
      className={`w-full rounded-md border p-3 text-left transition-colors ${
        selected ? "border-primary bg-primary/10" : "border-border bg-secondary/35 hover:bg-secondary"
      }`}
      onClick={onSelect}
    >
      <div className="flex items-start gap-3">
        <Icon className="mt-0.5 size-4 shrink-0 text-link" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-foreground">{artifact.filename}</p>
          <p className="mt-1 text-xs text-muted-foreground">{formatBytes(artifact.sizeBytes)} · {formatDateTime(artifact.createdAt)}</p>
          <div className="mt-2">
            <Badge tone="neutral">{titleCase(artifact.artifactType)}</Badge>
          </div>
        </div>
      </div>
    </button>
  )
}
