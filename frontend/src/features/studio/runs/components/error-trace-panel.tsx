"use client"

import { useState } from "react"
import { AlertTriangle } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Button } from "@/components/ui/button"
import { formatDateTime } from "@/lib/format"
import type { RunErrorTrace } from "@/types/agent"

export function ErrorTracePanel({ errors }: { errors: RunErrorTrace[] }) {
  if (!errors.length) return <EmptyState title="暂无错误" description="这次运行没有记录错误追踪。" />

  return (
    <div className="space-y-3">
      {errors.map((error) => (
        <ErrorTraceCard key={error.id} error={error} />
      ))}
    </div>
  )
}

function ErrorTraceCard({ error }: { error: RunErrorTrace }) {
  const [open, setOpen] = useState(false)

  return (
    <article className="rounded-md border border-destructive/30 bg-destructive/10 p-3">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-destructive">{error.message}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {formatDateTime(error.timestamp)} {error.stepId ? `· ${error.stepId}` : ""}
          </p>
          {error.retryHint ? <p className="mt-2 text-sm text-foreground">{error.retryHint}</p> : null}
        </div>
        {error.stackPreview ? (
          <Button type="button" variant="ghost" size="sm" onClick={() => setOpen((value) => !value)}>
            {open ? "收起" : "堆栈"}
          </Button>
        ) : null}
      </div>
      {open && error.stackPreview ? (
        <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background p-3 text-xs text-foreground">
          {error.stackPreview}
        </pre>
      ) : null}
    </article>
  )
}
