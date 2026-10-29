"use client"

import { useState } from "react"
import { Button } from "@/components/ui/button"
import { StudioCodeBlock } from "@/features/studio/shared/components/studio-dashboard"

export function JsonPreview({ value, label, defaultOpen = false }: { value?: unknown; label: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)

  if (value === undefined) {
    return (
      <div className="rounded-xl border border-border px-3 py-2.5 text-body2 text-muted-foreground">
        {label}：无
      </div>
    )
  }

  const json = typeof value === "string" ? value : JSON.stringify(value, null, 2)
  const preview = json.length > 600 && !open ? `${json.slice(0, 600)}\n...` : json

  return (
    <section className="space-y-1.5">
      <div className="flex items-center justify-between gap-3">
        <p className="text-label font-medium text-muted-foreground">{label}</p>
        <Button type="button" variant="ghost" size="xs" onClick={() => setOpen((value) => !value)}>
          {open ? "收起" : "展开"}
        </Button>
      </div>
      <StudioCodeBlock className="max-h-72 leading-5">{preview}</StudioCodeBlock>
    </section>
  )
}
