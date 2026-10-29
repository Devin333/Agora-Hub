"use client"

import { useState } from "react"
import { ArtifactViewer } from "@/features/studio/runs/components/artifact-viewer"
import { ErrorTracePanel } from "@/features/studio/runs/components/error-trace-panel"
import { JsonPreview } from "@/features/studio/runs/components/json-preview"
import { RunEventStream } from "@/features/studio/runs/components/run-event-stream"
import { RunOperationPanel } from "@/features/studio/runs/components/run-operation-panel"
import { RunQualityPanel } from "@/features/studio/runs/components/run-quality-panel"
import { ToolCallList } from "@/features/studio/runs/components/tool-call-list"
import { StudioSegmentTabs } from "@/features/studio/shared/components/studio-filters"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { AgentStep, StudioRunDetail } from "@/types/agent"

type RunCenterTab = "events" | "artifacts" | "quality" | "errors" | "diagnostics" | "health" | "operations" | "tools"

const tabs: Array<{ id: RunCenterTab; labelKey: string }> = [
  { id: "events", labelKey: "studio.runs.tab.events" },
  { id: "artifacts", labelKey: "studio.runs.tab.artifacts" },
  { id: "quality", labelKey: "studio.runs.tab.quality" },
  { id: "errors", labelKey: "studio.runs.tab.errors" },
  { id: "diagnostics", labelKey: "studio.runs.tab.diagnostics" },
  { id: "health", labelKey: "studio.runs.tab.health" },
  { id: "operations", labelKey: "studio.runs.tab.operations" },
  { id: "tools", labelKey: "studio.runs.tab.tools" }
]

export function RunDetailInspector({ detail, selectedStep }: { detail: StudioRunDetail; selectedStep?: AgentStep }) {
  const { t } = useI18n()
  const [activeTab, setActiveTab] = useState<RunCenterTab>(() => {
    if (detail.dataState === "fallback") return "events"
    if (detail.run.status === "blocked" || detail.run.status === "waiting_for_human") return "operations"
    if (detail.errors.length) return "errors"
    return "events"
  })

  return (
    <section className="min-w-0 overflow-hidden rounded-2xl border border-border bg-card">
      <div className="border-b border-border p-3">
        <StudioSegmentTabs tabs={tabs.map((tab) => ({ id: tab.id, label: t(tab.labelKey) }))} active={activeTab} onChange={setActiveTab} className="w-fit max-w-full" />
      </div>
      <div className="rk-scroll max-h-[760px] overflow-auto p-4">
        {activeTab === "events" ? <RunEventStream events={detail.events} /> : null}
        {activeTab === "artifacts" ? <ArtifactViewer artifacts={detail.artifacts} /> : null}
        {activeTab === "quality" ? <RunQualityPanel quality={detail.quality} /> : null}
        {activeTab === "errors" ? <ErrorTracePanel errors={detail.errors} /> : null}
        {activeTab === "diagnostics" ? <JsonPreview label={t("studio.runs.tab.diagnostics")} value={detail.diagnostics} defaultOpen /> : null}
        {activeTab === "health" ? <JsonPreview label={t("studio.runs.tab.health")} value={detail.health} defaultOpen /> : null}
        {activeTab === "operations" ? <RunOperationPanel detail={detail} selectedStep={selectedStep} /> : null}
        {activeTab === "tools" ? <ToolCallList calls={detail.toolCalls} /> : null}
      </div>
    </section>
  )
}
