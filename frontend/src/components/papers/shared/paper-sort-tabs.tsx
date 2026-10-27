"use client"

import { sortLabels, t } from "@/lib/papers/copy"
import type { Locale, PaperSort } from "@/lib/papers/types"
import { cn } from "@/lib/utils"

const sorts: PaperSort[] = ["trending", "newest", "most_cited"]

export function PaperSortTabs({
  value,
  locale,
  onChange
}: {
  value: PaperSort
  locale: Locale
  onChange: (sort: PaperSort) => void
}) {
  return (
    <div className="inline-flex h-8 items-center gap-0.5 rounded-lg bg-muted p-[3px]" role="group" aria-label="Paper sort">
      {sorts.map((sort) => (
        <button
          key={sort}
          type="button"
          aria-pressed={value === sort}
          className={cn(
            "h-full rounded-md border border-transparent px-2.5 text-body2 font-medium outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
            value === sort ? "border-input bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
          )}
          onClick={() => onChange(sort)}
        >
          {t(sortLabels[sort], locale)}
        </button>
      ))}
    </div>
  )
}
