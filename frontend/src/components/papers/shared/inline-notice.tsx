"use client"

import { papersCopy, t } from "@/lib/papers/copy"
import type { Locale } from "@/lib/papers/types"

export function InlineNotice({
  message,
  locale,
  onDismiss
}: {
  message: string | null
  locale: Locale
  onDismiss: () => void
}) {
  if (!message) {
    return null
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-link/30 bg-link/10 px-4 py-3 text-body2 text-foreground">
      <span>{message}</span>
      <button type="button" className="shrink-0 rounded-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50" onClick={onDismiss}>
        {t(papersCopy.dismiss, locale)}
      </button>
    </div>
  )
}
