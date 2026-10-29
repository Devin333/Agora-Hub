"use client"

import { useEffect, useMemo } from "react"
import { Badge } from "@/components/common/badge"
import { ErrorState } from "@/components/common/error-state"
import { EmptyState } from "@/components/common/empty-state"
import { PageHeader } from "@/components/layout/page-header"
import { RunDetailHeader } from "@/features/studio/runs/components/run-detail-header"
import { RunInspectorTabs } from "@/features/studio/runs/components/run-inspector-tabs"
import { StepDetailPanel } from "@/features/studio/runs/components/step-detail-panel"
import { StepTimeline } from "@/features/studio/runs/components/step-timeline"
import { WorkflowDag } from "@/features/studio/runs/components/workflow-dag"
import { useAgentRunDetail } from "@/features/studio/runs/hooks/use-agent-run-detail"
import { useRunInspectorStore } from "@/stores/run-inspector-store"
import type { AgentRunDetail } from "@/types/agent"

export function AgentRunDetailPage({ runId, detail }: { runId: string; detail?: AgentRunDetail }) {
  const { data, isError, error } = useAgentRunDetail(runId, detail)
  const selectedStepId = useRunInspectorStore((state) => state.selectedStepId)
  const selectStep = useRunInspectorStore((state) => state.selectStep)

  const defaultStep = useMemo(() => {
    if (!data?.steps.length) return undefined
    return data.steps.find((step) => step.status === "failed") ?? data.steps.find((step) => step.status === "running") ?? data.steps[0]
  }, [data])

  useEffect(() => {
    if (defaultStep && (!selectedStepId || !data?.steps.some((step) => step.id === selectedStepId))) {
      selectStep(defaultStep.nodeId, defaultStep.id)
    }
  }, [data?.steps, defaultStep, selectStep, selectedStepId])

  if (isError || !data) {
    return <ErrorState message={error instanceof Error ? error.message : "运行详情加载失败。"} />
  }

  const selectedStep = data.steps.find((step) => step.id === selectedStepId) ?? defaultStep

  return (
    <main className="space-y-5">
      <PageHeader
        eyebrow="Studio 运行"
        title="运行可观测性"
        description="检查工作流路径、步骤输入输出、工具调用、记忆命中、产物、质量检查和错误。"
      />
      <RunDetailHeader run={data.run} />

      {data.notices.length ? (
        <div className="flex flex-wrap gap-2">
          {data.notices.map((notice) => (
            <Badge key={notice} tone={data.dataState === "ready" ? "success" : "warning"}>{notice}</Badge>
          ))}
        </div>
      ) : null}

      {!data.steps.length ? (
        <EmptyState title="暂无步骤" description="这次运行没有步骤记录。" />
      ) : (
        <section className="grid gap-4 2xl:grid-cols-[minmax(300px,0.9fr)_minmax(360px,1fr)_minmax(360px,1fr)]">
          <div className="hidden 2xl:block">
            <WorkflowDag nodes={data.dag.nodes} edges={data.dag.edges} />
          </div>
          <div className="2xl:hidden">
            <StepTimeline steps={data.steps} />
          </div>
          <StepDetailPanel step={selectedStep} />
          <RunInspectorTabs detail={data} />
        </section>
      )}
    </main>
  )
}
