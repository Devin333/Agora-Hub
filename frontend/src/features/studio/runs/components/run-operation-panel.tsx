"use client"

import { useMemo, useState } from "react"
import { AlertTriangle, Ban, FastForward, RotateCcw, ShieldCheck } from "lucide-react"
import { Badge } from "@/components/common/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useRunOperations } from "@/features/studio/runs/hooks/use-run-operations"
import { useI18n } from "@/lib/i18n/use-i18n"
import clsx from "clsx"
import { cn } from "@/lib/utils"
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
        icon: <Ban size={16} strokeWidth={1.8} aria-hidden="true" />
      },
      {
        type: "rerun-from-step",
        label: t("studio.runs.rerunFromStep"),
        description: t("studio.runs.rerunFromStepDescription"),
        enabled: detail.operations.canRerunFromStep && Boolean(selectedStep),
        icon: <RotateCcw size={16} strokeWidth={1.8} aria-hidden="true" />
      },
      {
        type: "skip-step",
        label: t("studio.runs.skipStep"),
        description: t("studio.runs.skipStepDescription"),
        enabled: detail.operations.canSkipStep && Boolean(selectedStep),
        icon: <FastForward size={16} strokeWidth={1.8} aria-hidden="true" />
      },
      {
        type: "mark-blocked-resolved",
        label: t("studio.runs.markBlockedResolved"),
        description: t("studio.runs.markBlockedResolvedDescription"),
        enabled: detail.operations.canResolveBlocked,
        icon: <ShieldCheck size={16} strokeWidth={1.8} aria-hidden="true" />
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
    <section className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-semibold text-foreground">{t("studio.runs.operations")}</h2>
          <p className="mt-0.5 text-label text-muted-foreground">{t("studio.runs.operationsDescription")}</p>
        </div>
        {detail.dataState === "fallback" ? <Badge tone="warning">{t("studio.runs.disabledFallback")}</Badge> : null}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {operations.map((item) => (
          <button
            key={item.type}
            type="button"
            disabled={!item.enabled}
            aria-pressed={operation === item.type}
            className={cn(
              "rounded-xl border border-border px-3 py-2.5 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50",
              operation === item.type ? "bg-accent" : "enabled:hover:bg-accent/40"
            )}
            onClick={() => {
              setOperation(item.type)
              clearResult()
            }}
          >
            <span className="flex items-center gap-2 text-[13.5px] font-medium text-foreground">
              {item.icon}
              {item.label}
            </span>
            <span className="mt-0.5 block text-label text-muted-foreground">{item.description}</span>
          </button>
        ))}
      </div>

      <form className="space-y-3" onSubmit={onSubmit}>
        {requiresStep ? (
          <div className="rounded-xl border border-border px-3 py-2.5">
            <p className="text-label text-muted-foreground">{t("studio.runs.selectedStep")}</p>
            <p className="mt-0.5 font-mono text-label text-foreground">{selectedStep?.id ?? t("studio.runs.noStepSelected")}</p>
          </div>
        ) : null}
        <label className="grid gap-1 text-label text-muted-foreground">
          {t("studio.runs.reason")}
          <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder={t("studio.runs.reasonPlaceholder")} />
        </label>
        <label className="grid gap-1 text-label text-muted-foreground">
          {t("studio.runs.actorId")}
          <Input value={actorId} onChange={(event) => setActorId(event.target.value)} placeholder={t("studio.runs.actorPlaceholder")} />
        </label>
        <label className="flex cursor-pointer items-start gap-2 rounded-xl border border-warning/40 bg-warning/10 px-3 py-2.5 text-body2 text-warning-text">
          <input className="mt-0.5 accent-primary" type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
          {t("studio.runs.confirmRuntime")}
        </label>

        {!active.enabled ? (
          <p className="flex items-center gap-2 text-body2 text-muted-foreground">
            <AlertTriangle size={16} strokeWidth={1.8} aria-hidden="true" />
            {t("studio.runs.operationUnavailable")}
          </p>
        ) : null}

        {result ? (
          <div role="status" className={clsx("rounded-xl border px-3 py-2.5 text-body2", result.ok ? "border-success/40 bg-success/10 text-success-text" : "border-destructive/30 bg-destructive/10 text-destructive-text")}>
            <p>{result.ok ? result.message ?? t("studio.runs.operationAccepted") : result.message ?? t("studio.runs.operationFailed")}</p>
            {!result.ok && result.requestId ? <p className="mt-1 font-mono text-label">requestId: {result.requestId}</p> : null}
          </div>
        ) : null}

        <Button type="submit" variant={operation === "cancel" ? "destructive" : "default"} disabled={!canSubmit}>
          {isPending ? t("studio.runs.submitting") : active.label}
        </Button>
      </form>
    </section>
  )
}
