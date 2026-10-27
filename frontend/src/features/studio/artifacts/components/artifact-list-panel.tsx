"use client"

import Link from "next/link"
import { useMemo, useState } from "react"
import { EmptyState } from "@/components/common/empty-state"
import { SearchField } from "@/components/common/search-field"
import { Badge } from "@/components/ui/badge"
import { StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { formatBytes } from "@/lib/format"
import { useI18n } from "@/lib/i18n/use-i18n"
import { cn } from "@/lib/utils"
import type { StudioArtifact } from "@/types/artifact"

export function ArtifactListPanel({
  runId,
  artifacts,
  selectedArtifactKey
}: {
  runId: string
  artifacts: StudioArtifact[]
  selectedArtifactKey?: string
}) {
  const { t } = useI18n()
  const [query, setQuery] = useState("")
  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return artifacts
    return artifacts.filter((artifact) =>
      [artifact.artifactKey, artifact.relativePath, artifact.contentType, artifact.previewKind]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(normalized)
    )
  }, [artifacts, query])

  return (
    <StudioPanel title={t("studio.artifacts.list")} contentClassName="space-y-3">
      <SearchField
        aria-label={t("studio.artifacts.searchPlaceholder")}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder={t("studio.artifacts.searchPlaceholder")}
      />

      {!filtered.length ? (
        <EmptyState title={t("studio.artifacts.empty")} description={t("studio.artifacts.emptyDescription")} />
      ) : (
        <div className="rk-scroll max-h-[36rem] divide-y divide-border overflow-auto rounded-xl border border-border">
          {filtered.map((artifact) => {
            const selected = artifact.artifactKey === selectedArtifactKey
            return (
              <Link
                key={artifact.artifactKey}
                href={`/studio/artifacts/runs/${encodeURIComponent(runId)}?artifact=${encodeURIComponent(artifact.artifactKey)}`}
                aria-current={selected ? "true" : undefined}
                className={cn(
                  "block px-4 py-3 outline-none transition-colors focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50",
                  selected ? "bg-accent" : "hover:bg-accent/40"
                )}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-[13.5px] font-medium text-foreground">{artifact.artifactKey}</p>
                    <p className="mt-0.5 truncate text-label text-muted-foreground">{artifact.relativePath ?? t("studio.artifacts.noPath")}</p>
                  </div>
                  <Badge variant={artifact.readError ? "danger" : "info"}>{artifact.previewKind}</Badge>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-1 text-label text-faint-foreground">
                  <span>{artifact.contentType ?? "unknown"}</span>
                  <span className="tabular-nums">{formatBytes(artifact.sizeBytes)}</span>
                  {artifact.truncated ? <span>{t("studio.artifacts.truncated")}</span> : null}
                  {artifact.redacted ? <span>{t("studio.artifacts.redacted")}</span> : null}
                </div>
              </Link>
            )
          })}
        </div>
      )}
    </StudioPanel>
  )
}
