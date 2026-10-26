"use client"

import { FileArchive, FileCode2, FileJson2, FileText } from "lucide-react"
import { MarkdownViewer } from "@/components/markdown/markdown-viewer"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatBytes } from "@/lib/format"
import type { StudioArtifact } from "@/types/artifact"

export function ArtifactPreview({ artifact }: { artifact?: StudioArtifact }) {
  if (!artifact) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>产物预览</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">选择一个 artifact 查看内容和 metadata。</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle className="truncate">{artifact.artifactKey}</CardTitle>
          <p className="mt-1 truncate text-sm text-muted-foreground">{artifact.relativePath ?? "无路径"}</p>
        </div>
        <Badge variant={artifact.readError ? "danger" : "info"}>{artifact.previewKind}</Badge>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <PreviewMeta label="Run" value={artifact.runId} />
          <PreviewMeta label="Content-Type" value={artifact.contentType ?? "unknown"} />
          <PreviewMeta label="Size" value={formatBytes(artifact.sizeBytes)} />
          <PreviewMeta label="Redaction" value={artifact.redacted ? "含脱敏内容" : "未标记"} />
        </div>

        {artifact.readError ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{artifact.readError}</div>
        ) : null}
        {artifact.previewNotice ? (
          <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">{artifact.previewNotice}</div>
        ) : null}

        <PreviewBody artifact={artifact} />
      </CardContent>
    </Card>
  )
}

function PreviewBody({ artifact }: { artifact: StudioArtifact }) {
  if (artifact.previewKind === "binary") {
    return (
      <div className="flex min-h-48 flex-col items-center justify-center rounded-md border border-dashed border-border bg-secondary/30 p-6 text-center">
        <FileArchive className="mb-3 size-8 text-muted-foreground" />
        <p className="text-sm font-medium text-foreground">二进制产物不渲染正文</p>
        <p className="mt-1 text-sm text-muted-foreground">请通过 metadata、大小和路径判断该产物。</p>
      </div>
    )
  }

  if (!artifact.previewText) {
    return (
      <div className="flex min-h-32 flex-col items-center justify-center rounded-md border border-dashed border-border bg-secondary/30 p-6 text-center">
        <FileText className="mb-3 size-7 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">暂无可预览内容。</p>
      </div>
    )
  }

  if (artifact.previewKind === "markdown") {
    return <MarkdownViewer markdown={artifact.previewText} />
  }

  const icon = artifact.previewKind === "json" ? FileJson2 : artifact.previewKind === "html" ? FileCode2 : FileText
  const Icon = icon

  return (
    <div className="rounded-md border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
        <Icon className="size-4" />
        <span>{artifact.previewKind === "html" ? "HTML escaped preview" : "Raw preview"}</span>
      </div>
      <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words p-4 text-xs leading-6 text-foreground">
        {artifact.previewText}
      </pre>
    </div>
  )
}

function PreviewMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-xs uppercase text-muted-foreground">{label}</p>
      <p className="mt-1 truncate font-medium text-foreground">{value}</p>
    </div>
  )
}
