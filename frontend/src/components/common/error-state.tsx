"use client"

import { AlertTriangle, RefreshCcw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/lib/i18n/use-i18n"

export type ErrorStateProps = {
  title?: string
  message?: string
  onRetry?: () => void
}

export function ErrorState({ title, message, onRetry }: ErrorStateProps) {
  const { t } = useI18n()

  return (
    <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-5 shrink-0 text-destructive" />
        <div className="min-w-0 flex-1">
          <p className="font-semibold text-destructive">{title ?? t("common.error")}</p>
          {message ? <p className="mt-1 leading-6 text-foreground">{message}</p> : null}
          {onRetry ? (
            <Button className="mt-3" variant="outline" size="sm" onClick={onRetry}>
              <RefreshCcw className="size-4" />
              {t("common.retry")}
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  )
}
