import { FileJson, FileText } from "lucide-react"
import { Badge } from "@/components/common/badge"
import { formatBytes } from "@/features/studio/runs/lib/run-format"
import { formatDateTime, titleCase } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { Artifact } from "@/types/agent"

export function ArtifactCard({ artifact, selected, onSelect }: { artifact: Artifact; selected: boolean; onSelect: () => void }) {
  const Icon = artifact.artifactType === "markdown" || artifact.artifactType === "report" ? FileText : FileJson

  return (
    <button
      type="button"
      aria-pressed={selected}
      className={cn(
        "w-full rounded-xl border px-3 py-2.5 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
        selected ? "border-border bg-accent" : "border-border hover:bg-accent/40"
      )}
      onClick={onSelect}
    >
      <div className="flex items-start gap-3">
        <Icon size={16} strokeWidth={1.8} aria-hidden="true" className="mt-0.5 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13.5px] font-medium text-foreground">{artifact.filename}</p>
          <p className="mt-0.5 text-label text-muted-foreground">{formatBytes(artifact.sizeBytes)} · {formatDateTime(artifact.createdAt)}</p>
          <div className="mt-2">
            <Badge tone="neutral">{titleCase(artifact.artifactType)}</Badge>
          </div>
        </div>
      </div>
    </button>
  )
}
