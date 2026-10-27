"use client"

import { RefreshCcw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/lib/i18n/use-i18n"

export type ErrorStateProps = {
  title?: string
  message?: string
  onRetry?: () => void
}

/** Tinted destructive notice rakazo uses for load failures. */
export function ErrorState({ title, message, onRetry }: ErrorStateProps) {
  const { t } = useI18n()

  return (
    <div role="alert" className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-body2">
      <p className="font-medium text-destructive">{title ?? t("common.error")}</p>
      {message ? <p className="mt-1 text-foreground/80">{message}</p> : null}
      {onRetry ? (
        <Button className="mt-3" variant="outline" size="sm" onClick={onRetry}>
          <RefreshCcw strokeWidth={1.8} />
          {t("common.retry")}
        </Button>
      ) : null}
    </div>
  )
}
