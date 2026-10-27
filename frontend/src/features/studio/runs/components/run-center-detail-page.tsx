"use client"

import { useEffect, useMemo } from "react"
import { Badge } from "@/components/common/badge"
import { ErrorState } from "@/components/common/error-state"
import { EmptyState } from "@/components/common/empty-state"
import { PageHeader } from "@/components/layout/page-header"
import { RunDetailHeader } from "@/features/studio/runs/components/run-detail-header"
import { RunDetailInspector } from "@/features/studio/runs/components/run-detail-inspector"
import { StepDetailPanel } from "@/features/studio/runs/components/step-detail-panel"
import { StepTimeline } from "@/features/studio/runs/components/step-timeline"
import { WorkflowDag } from "@/features/studio/runs/components/workflow-dag"
import { useRunDetail } from "@/features/studio/runs/hooks/use-run-detail"
import { useI18n } from "@/lib/i18n/use-i18n"
import { useRunInspectorStore } from "@/stores/run-inspector-store"
import type { StudioRunDetail } from "@/types/agent"

export function RunCenterDetailPage({ runId, detail }: { runId: string; detail?: StudioRunDetail }) {
  const { t } = useI18n()
  const { data, isError, error } = useRunDetail(runId, detail)
  const selectedStepId = useRunInspectorStore((state) => state.selectedStepId)
  const selectStep = useRunInspectorStore((state) => state.selectStep)

  const defaultStep = useMemo(() => {
    if (!data?.steps.length) return undefined
    return data.steps.find((step) => step.status === "failed" || step.status === "blocked") ?? data.steps.find((step) => step.status === "running") ?? data.steps[0]
  }, [data])

  useEffect(() => {
    if (defaultStep && (!selectedStepId || !data?.steps.some((step) => step.id === selectedStepId))) {
      selectStep(defaultStep.nodeId, defaultStep.id)
    }
  }, [data?.steps, defaultStep, selectStep, selectedStepId])

  if (isError || !data) {
    return <ErrorState message={error instanceof Error ? error.message : t("studio.runs.detailFailed")} />
  }

  const selectedStep = data.steps.find((step) => step.id === selectedStepId) ?? defaultStep

  return (
    <main className="space-y-5">
      <PageHeader
        eyebrow={t("studio.runs.detailEyebrow")}
        title={t("studio.runs.observability")}
        description={t("studio.runs.observabilityDescription")}
      />
      <RunDetailHeader run={data.run} />

      {data.notices.length ? (
        <div className="flex flex-wrap gap-2">
          {data.notices.map((notice) => (
            <Badge key={notice} tone={data.dataState === "ready" ? "success" : "warning"}>{notice}</Badge>
          ))}
        </div>
      ) : null}

      {data.run.artifactDir || data.run.manifestPath || data.run.reportId ? (
        <section className="grid gap-3 rounded-2xl border border-border bg-card p-4 text-body2 md:grid-cols-3">
          <Meta label={t("studio.runs.report")} value={data.run.reportId} empty={t("common.none")} />
          <Meta label={t("studio.runs.artifactDirectory")} value={data.run.artifactDir} empty={t("common.none")} />
          <Meta label={t("studio.runs.manifest")} value={data.run.manifestPath} empty={t("common.none")} />
        </section>
      ) : null}

      {!data.steps.length ? (
        <EmptyState title={t("studio.runs.noSteps")} description={t("studio.runs.noStepsDescription")} />
      ) : (
        <section className="grid gap-4 2xl:grid-cols-[minmax(300px,0.9fr)_minmax(360px,1fr)_minmax(420px,1.1fr)]">
          <div className="hidden 2xl:block">
            <WorkflowDag nodes={data.dag.nodes} edges={data.dag.edges} />
          </div>
          <div className="2xl:hidden">
            <StepTimeline steps={data.steps} />
          </div>
          <StepDetailPanel step={selectedStep} />
          <RunDetailInspector detail={data} selectedStep={selectedStep} />
        </section>
      )}
    </main>
  )
}

function Meta({ label, value, empty }: { label: string; value?: string; empty: string }) {
  return (
    <div className="min-w-0">
      <p className="text-label text-muted-foreground">{label}</p>
      <p className="mt-0.5 truncate font-mono text-label text-foreground">{value ?? empty}</p>
    </div>
  )
}
