"use client"

import { ArtifactViewer } from "@/features/studio/runs/components/artifact-viewer"
import { ErrorTracePanel } from "@/features/studio/runs/components/error-trace-panel"
import { MemoryHitList } from "@/features/studio/runs/components/memory-hit-list"
import { RunLogList } from "@/features/studio/runs/components/run-log-list"
import { RunQualityPanel } from "@/features/studio/runs/components/run-quality-panel"
import { ToolCallList } from "@/features/studio/runs/components/tool-call-list"
import { StudioSegmentTabs } from "@/features/studio/shared/components/studio-filters"
import { useRunInspectorStore, type InspectorTab } from "@/stores/run-inspector-store"
import type { AgentRunDetail } from "@/types/agent"

const tabs: Array<{ id: InspectorTab; label: string }> = [
  { id: "logs", label: "日志" },
  { id: "tools", label: "工具调用" },
  { id: "memory", label: "记忆命中" },
  { id: "artifacts", label: "产物" },
  { id: "quality", label: "质量" },
  { id: "errors", label: "错误" }
]

export function RunInspectorTabs({ detail }: { detail: AgentRunDetail }) {
  const activeTab = useRunInspectorStore((state) => state.activeTab)
  const setActiveTab = useRunInspectorStore((state) => state.setActiveTab)

  return (
    <section className="min-w-0 overflow-hidden rounded-2xl border border-border bg-card">
      <div className="border-b border-border p-3">
        <StudioSegmentTabs tabs={tabs} active={activeTab} onChange={setActiveTab} className="w-fit max-w-full" />
      </div>
      <div className="rk-scroll max-h-[720px] overflow-auto p-4">
        {activeTab === "logs" ? <RunLogList logs={detail.logs} /> : null}
        {activeTab === "tools" ? <ToolCallList calls={detail.toolCalls} /> : null}
        {activeTab === "memory" ? <MemoryHitList hits={detail.memoryHits} /> : null}
        {activeTab === "artifacts" ? <ArtifactViewer artifacts={detail.artifacts} /> : null}
        {activeTab === "quality" ? <RunQualityPanel quality={detail.quality} /> : null}
        {activeTab === "errors" ? <ErrorTracePanel errors={detail.errors} /> : null}
      </div>
    </section>
  )
}
