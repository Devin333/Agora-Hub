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
    <div className="flex items-center justify-between gap-3 rounded-md border border-link/30 bg-link/10 px-4 py-3 text-sm text-foreground">
      <span>{message}</span>
      <button type="button" className="text-muted-foreground hover:text-foreground" onClick={onDismiss}>
        {t(papersCopy.dismiss, locale)}
      </button>
    </div>
  )
}
