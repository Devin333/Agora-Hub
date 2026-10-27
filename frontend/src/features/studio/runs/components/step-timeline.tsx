"use client"

import { Badge } from "@/components/common/badge"
import { formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import { cn } from "@/lib/utils"
import { useRunInspectorStore } from "@/stores/run-inspector-store"
import type { AgentStep } from "@/types/agent"

export function StepTimeline({ steps }: { steps: AgentStep[] }) {
  const selectedStepId = useRunInspectorStore((state) => state.selectedStepId)
  const selectStep = useRunInspectorStore((state) => state.selectStep)
  const setActiveTab = useRunInspectorStore((state) => state.setActiveTab)

  return (
    <ol className="divide-y divide-border overflow-hidden rounded-xl border border-border">
      {steps.map((step) => (
        <li key={step.id}>
          <button
            type="button"
            aria-pressed={selectedStepId === step.id}
            className={cn(
              "w-full px-4 py-3 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50",
              selectedStepId === step.id ? "bg-accent" : "hover:bg-accent/40"
            )}
            onClick={() => {
              selectStep(step.nodeId, step.id)
              if (step.status === "failed") setActiveTab("errors")
            }}
          >
            <div className="flex items-center justify-between gap-3">
              <span className="truncate text-[13.5px] font-medium text-foreground">{step.label}</span>
              <Badge tone={statusTone(step.status)}>{formatRunStatus(step.status)}</Badge>
            </div>
            <p className="mt-0.5 text-label text-muted-foreground">{step.type}</p>
          </button>
        </li>
      ))}
    </ol>
  )
}
