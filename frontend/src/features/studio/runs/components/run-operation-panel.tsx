"use client"

import { useMemo, useState } from "react"
import { AlertTriangle, Ban, FastForward, RotateCcw, ShieldCheck } from "lucide-react"
import { Badge } from "@/components/common/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useRunOperations } from "@/features/studio/runs/hooks/use-run-operations"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { AgentStep, RunOperationType, StudioRunDetail } from "@/types/agent"

type OperationOption = {
  type: RunOperationType
  label: string
  description: string
  enabled: boolean
  icon: React.ReactNode
}

export function RunOperationPanel({
  detail,
  selectedStep
}: {
  detail: StudioRunDetail
  selectedStep?: AgentStep
}) {
  const { t } = useI18n()
  const { execute, isPending, result, clearResult } = useRunOperations(detail.run.id)
  const [operation, setOperation] = useState<RunOperationType>("cancel")
  const [reason, setReason] = useState("")
  const [confirmed, setConfirmed] = useState(false)
  const [actorId, setActorId] = useState("")

  const operations = useMemo<OperationOption[]>(
    () => [
      {
        type: "cancel",
        label: t("studio.runs.cancel"),
        description: t("studio.runs.cancelDescription"),
        enabled: detail.operations.canCancel,
        icon: <Ban className="size-4" />
      },
      {
        type: "rerun-from-step",
        label: t("studio.runs.rerunFromStep"),
        description: t("studio.runs.rerunFromStepDescription"),
        enabled: detail.operations.canRerunFromStep && Boolean(selectedStep),
        icon: <RotateCcw className="size-4" />
      },
      {
        type: "skip-step",
        label: t("studio.runs.skipStep"),
        description: t("studio.runs.skipStepDescription"),
        enabled: detail.operations.canSkipStep && Boolean(selectedStep),
        icon: <FastForward className="size-4" />
      },
      {
        type: "mark-blocked-resolved",
        label: t("studio.runs.markBlockedResolved"),
        description: t("studio.runs.markBlockedResolvedDescription"),
        enabled: detail.operations.canResolveBlocked,
        icon: <ShieldCheck className="size-4" />
      }
    ],
    [detail.operations, selectedStep, t]
  )

  const active = operations.find((item) => item.type === operation) ?? operations[0]
  const requiresStep = operation === "rerun-from-step" || operation === "skip-step"
  const canSubmit = active.enabled && reason.trim().length > 0 && confirmed && (!requiresStep || Boolean(selectedStep)) && !isPending

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    clearResult()
    await execute(operation, {
      reason: reason.trim(),
      stepId: selectedStep?.id,
      actorId: actorId.trim() || undefined,
      resolvedBy: actorId.trim() || undefined
    })
  }

  return (
    <section className="space-y-4 rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-foreground">{t("studio.runs.operations")}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{t("studio.runs.operationsDescription")}</p>
        </div>
        {detail.dataState === "fallback" ? <Badge tone="warning">{t("studio.runs.disabledFallback")}</Badge> : null}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {operations.map((item) => (
          <button
            key={item.type}
            type="button"
            disabled={!item.enabled}
            className={`rounded-md border p-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              operation === item.type ? "border-primary bg-primary/10" : "border-border bg-secondary/35 hover:bg-secondary"
            }`}
            onClick={() => {
              setOperation(item.type)
              clearResult()
            }}
          >
            <span className="flex items-center gap-2 text-sm font-medium text-foreground">
              {item.icon}
              {item.label}
            </span>
            <span className="mt-1 block text-xs text-muted-foreground">{item.description}</span>
          </button>
        ))}
      </div>

      <form className="space-y-3" onSubmit={onSubmit}>
        {requiresStep ? (
          <div className="rounded-md border border-border bg-secondary/35 p-3">
            <p className="text-xs font-medium uppercase tracking-normal text-muted-foreground">{t("studio.runs.selectedStep")}</p>
            <p className="mt-1 font-mono text-xs text-foreground">{selectedStep?.id ?? t("studio.runs.noStepSelected")}</p>
          </div>
        ) : null}
        <label className="grid gap-1 text-sm text-muted-foreground">
          {t("studio.runs.reason")}
          <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder={t("studio.runs.reasonPlaceholder")} />
        </label>
        <label className="grid gap-1 text-sm text-muted-foreground">
          {t("studio.runs.actorId")}
          <Input value={actorId} onChange={(event) => setActorId(event.target.value)} placeholder={t("studio.runs.actorPlaceholder")} />
        </label>
        <label className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
          <input className="mt-0.5" type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
          {t("studio.runs.confirmRuntime")}
        </label>

        {!active.enabled ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <AlertTriangle className="size-4" />
            {t("studio.runs.operationUnavailable")}
          </p>
        ) : null}

        {result ? (
          <div className={`rounded-md border p-3 text-sm ${result.ok ? "border-success/30 bg-success/10 text-success" : "border-destructive/30 bg-destructive/10 text-destructive"}`}>
            <p>{result.ok ? result.message ?? t("studio.runs.operationAccepted") : result.message ?? t("studio.runs.operationFailed")}</p>
            {!result.ok && result.requestId ? <p className="mt-1 font-mono text-xs">requestId: {result.requestId}</p> : null}
          </div>
        ) : null}

        <Button type="submit" variant={operation === "cancel" ? "destructive" : "default"} disabled={!canSubmit}>
          {isPending ? t("studio.runs.submitting") : active.label}
        </Button>
      </form>
    </section>
  )
}
