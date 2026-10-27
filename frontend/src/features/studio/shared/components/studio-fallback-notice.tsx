"use client"

import { AlertTriangle } from "lucide-react"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioFallbackNotice as StudioFallbackNoticeProps } from "@/types/studio"

export function StudioFallbackNotice({
  title,
  message,
  requestId,
  error,
  action
}: StudioFallbackNoticeProps) {
  const { t } = useI18n()
  const resolvedRequestId = requestId ?? error?.requestId

  return (
    <section role="status" className="rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 text-body2">
      <div className="flex items-start gap-3">
        <AlertTriangle size={16} strokeWidth={1.8} className="mt-0.5 shrink-0 text-warning" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-warning-text">{title ?? t("studio.fallback.title")}</p>
          <p className="mt-1 text-foreground/80">{message}</p>
          {resolvedRequestId ? (
            <p className="mt-2 font-mono text-label text-muted-foreground">request_id={resolvedRequestId}</p>
          ) : null}
          {action ? <div className="mt-3">{action}</div> : null}
        </div>
      </div>
    </section>
  )
}
