"use client"

import { useState } from "react"
import { AlertTriangle } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Button } from "@/components/ui/button"
import { StudioCodeBlock } from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime } from "@/lib/format"
import type { RunErrorTrace } from "@/types/agent"

export function ErrorTracePanel({ errors }: { errors: RunErrorTrace[] }) {
  if (!errors.length) return <EmptyState title="暂无错误" description="这次运行没有记录错误追踪。" />

  return (
    <div className="space-y-2">
      {errors.map((error) => (
        <ErrorTraceCard key={error.id} error={error} />
      ))}
    </div>
  )
}

function ErrorTraceCard({ error }: { error: RunErrorTrace }) {
  const [open, setOpen] = useState(false)

  return (
    <article className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
      <div className="flex items-start gap-3">
        <AlertTriangle size={16} strokeWidth={1.8} aria-hidden="true" className="mt-0.5 shrink-0 text-destructive" />
        <div className="min-w-0 flex-1">
          <p className="text-[13.5px] font-medium text-destructive-text">{error.message}</p>
          <p className="mt-0.5 text-label text-muted-foreground">
            {formatDateTime(error.timestamp)} {error.stepId ? `· ${error.stepId}` : ""}
          </p>
          {error.retryHint ? <p className="mt-2 text-body2 text-foreground/90">{error.retryHint}</p> : null}
        </div>
        {error.stackPreview ? (
          <Button type="button" variant="ghost" size="xs" onClick={() => setOpen((value) => !value)}>
            {open ? "收起" : "堆栈"}
          </Button>
        ) : null}
      </div>
      {open && error.stackPreview ? <StudioCodeBlock className="mt-3 max-h-72 bg-background">{error.stackPreview}</StudioCodeBlock> : null}
    </article>
  )
}
