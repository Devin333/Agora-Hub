"use client"

import { useState } from "react"
import { EmptyState } from "@/components/common/empty-state"
import { Button } from "@/components/ui/button"
import { ArtifactCard } from "@/features/studio/runs/components/artifact-card"
import type { Artifact } from "@/types/agent"

export function ArtifactViewer({ artifacts }: { artifacts: Artifact[] }) {
  const [selectedId, setSelectedId] = useState(artifacts[0]?.id)
  const selected = artifacts.find((artifact) => artifact.id === selectedId) ?? artifacts[0]

  if (!artifacts.length) return <EmptyState title="暂无产物" description="这次运行没有生成产物。" />

  return (
    <div className="grid gap-3 lg:grid-cols-[260px_minmax(0,1fr)]">
      <div className="space-y-2">
        {artifacts.map((artifact) => (
          <ArtifactCard key={artifact.id} artifact={artifact} selected={selected?.id === artifact.id} onSelect={() => setSelectedId(artifact.id)} />
        ))}
      </div>
      <section className="min-w-0 overflow-hidden rounded-xl border border-border">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <p className="truncate text-[13.5px] font-medium text-foreground">{selected.filename}</p>
          <Button type="button" variant="outline" size="sm" disabled>
            下载
          </Button>
        </div>
        <pre className="rk-scroll max-h-[480px] overflow-auto whitespace-pre-wrap break-words bg-muted p-3 font-mono text-label leading-5 text-foreground/90">
          {selected.preview ?? "暂无预览。接入产物读取 API 后会获取真实产物内容。"}
        </pre>
      </section>
    </div>
  )
}
