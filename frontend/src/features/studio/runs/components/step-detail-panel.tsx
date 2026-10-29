import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { JsonPreview } from "@/features/studio/runs/components/json-preview"
import { formatDuration, formatRunStatus, statusTone } from "@/features/studio/runs/lib/run-format"
import { formatDateTime } from "@/lib/format"
import type { AgentStep } from "@/types/agent"

export function StepDetailPanel({ step }: { step?: AgentStep }) {
  if (!step) {
    return <EmptyState title="未选择步骤" description="选择工作流节点或时间线条目，以检查输入、输出、产物和错误。" />
  }

  return (
    <section className="min-w-0 space-y-3 rounded-2xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-[15px] font-semibold text-foreground">{step.label}</h2>
          <p className="mt-0.5 font-mono text-label text-muted-foreground">{step.id}</p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Badge tone={statusTone(step.status)}>{formatRunStatus(step.status)}</Badge>
          <Badge tone="neutral">{step.type}</Badge>
        </div>
      </div>

      {step.errorMessage ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-body2 text-destructive-text">{step.errorMessage}</div>
      ) : null}

      <dl className="grid gap-2 sm:grid-cols-3">
        <Field label="节点" value={step.nodeId} />
        <Field label="开始" value={formatDateTime(step.startedAt)} />
        <Field label="耗时" value={formatDuration(step.durationMs)} />
      </dl>

      <JsonPreview label="输入预览" value={step.inputPreview} />
      <JsonPreview label="输出预览" value={step.outputPreview} />

      <div className="rounded-xl border border-border px-3 py-2.5">
        <p className="text-label text-muted-foreground">产物</p>
        {step.artifactIds?.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {step.artifactIds.map((artifactId) => (
              <Badge key={artifactId} tone="accent">{artifactId}</Badge>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-body2 text-muted-foreground">这个步骤没有附加产物。</p>
        )}
      </div>
    </section>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-border px-3 py-2.5">
      <dt className="text-label text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 truncate text-[13.5px] font-medium text-foreground">{value}</dd>
    </div>
  )
}
