"use client"

import { FileArchive, FileCode2, FileJson2, FileText } from "lucide-react"
import { MarkdownViewer } from "@/components/markdown/markdown-viewer"
import { Badge } from "@/components/ui/badge"
import { StudioField, StudioFieldGrid, StudioNotice, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { formatBytes } from "@/lib/format"
import type { StudioArtifact } from "@/types/artifact"

export function ArtifactPreview({ artifact }: { artifact?: StudioArtifact }) {
  if (!artifact) {
    return (
      <StudioPanel title="产物预览">
        <p className="text-body2 text-muted-foreground">选择一个 artifact 查看内容和 metadata。</p>
      </StudioPanel>
    )
  }

  return (
    <StudioPanel
      title={artifact.artifactKey}
      description={artifact.relativePath ?? "无路径"}
      actions={<Badge variant={artifact.readError ? "danger" : "info"}>{artifact.previewKind}</Badge>}
      contentClassName="space-y-3"
    >
      <StudioFieldGrid className="lg:grid-cols-4 xl:grid-cols-4">
        <StudioField label="Run" value={<span className="block truncate">{artifact.runId}</span>} />
        <StudioField label="Content-Type" value={<span className="block truncate">{artifact.contentType ?? "unknown"}</span>} />
        <StudioField label="Size" value={formatBytes(artifact.sizeBytes)} />
        <StudioField label="Redaction" value={artifact.redacted ? "含脱敏内容" : "未标记"} />
      </StudioFieldGrid>

      {artifact.readError ? <StudioNotice tone="danger">{artifact.readError}</StudioNotice> : null}
      {artifact.previewNotice ? <StudioNotice>{artifact.previewNotice}</StudioNotice> : null}

      <PreviewBody artifact={artifact} />
    </StudioPanel>
  )
}

function PreviewBody({ artifact }: { artifact: StudioArtifact }) {
  if (artifact.previewKind === "binary") {
    return (
      <div className="flex min-h-48 flex-col items-center justify-center px-6 py-8 text-center">
        <FileArchive size={20} strokeWidth={1.8} aria-hidden="true" className="mb-3 text-muted-foreground" />
        <p className="text-body2 font-medium text-foreground">二进制产物不渲染正文</p>
        <p className="mt-1 text-label text-muted-foreground">请通过 metadata、大小和路径判断该产物。</p>
      </div>
    )
  }

  if (!artifact.previewText) {
    return (
      <div className="flex min-h-32 flex-col items-center justify-center px-6 py-8 text-center">
        <FileText size={18} strokeWidth={1.8} aria-hidden="true" className="mb-3 text-muted-foreground" />
        <p className="text-body2 text-muted-foreground">暂无可预览内容。</p>
      </div>
    )
  }

  if (artifact.previewKind === "markdown") {
    return <MarkdownViewer markdown={artifact.previewText} />
  }

  const Icon = artifact.previewKind === "json" ? FileJson2 : artifact.previewKind === "html" ? FileCode2 : FileText

  return (
    <div className="overflow-hidden rounded-xl border border-border">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-label font-medium text-muted-foreground">
        <Icon size={15} strokeWidth={1.8} aria-hidden="true" />
        <span>{artifact.previewKind === "html" ? "HTML escaped preview" : "Raw preview"}</span>
      </div>
      <pre className="rk-scroll max-h-[32rem] overflow-auto whitespace-pre-wrap break-words bg-muted p-3 font-mono text-label leading-6 text-foreground/90">
        {artifact.previewText}
      </pre>
    </div>
  )
}
