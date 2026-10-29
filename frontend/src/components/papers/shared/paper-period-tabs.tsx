"use client"

import { periodLabels, t } from "@/lib/papers/copy"
import type { Locale, PaperPeriod } from "@/lib/papers/types"
import { cn } from "@/lib/utils"

const periods: PaperPeriod[] = ["daily", "weekly", "monthly", "all"]

export function PaperPeriodTabs({
  value,
  locale,
  hrefForPeriod,
  onChange,
  fullWidth = false
}: {
  value: PaperPeriod
  locale: Locale
  hrefForPeriod?: (period: PaperPeriod) => string
  onChange: (period: PaperPeriod) => void
  fullWidth?: boolean
}) {
  return (
    <div
      className={cn(
        "h-8 items-center gap-0.5 rounded-lg bg-muted p-[3px] text-muted-foreground",
        fullWidth ? "grid w-full grid-cols-4" : "inline-flex"
      )}
      role="group"
      aria-label="Paper period"
    >
      {periods.map((period) => {
        const href = hrefForPeriod?.(period) ?? "#"
        const formId = `paper-period-${period}`
        return (
          <span key={period} className={cn("inline-flex h-full", fullWidth && "min-w-0")}>
            <form id={formId} action={actionPath(href)} method="get" className="hidden" aria-hidden="true">
              {hiddenFields(href).map(([name, hiddenValue]) => (
                <input key={name} type="hidden" name={name} value={hiddenValue} />
              ))}
            </form>
            <button
              type="submit"
              form={formId}
              aria-pressed={value === period}
              className={cn(
                "h-full truncate rounded-md border border-transparent px-2.5 text-body2 font-medium outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
                fullWidth && "w-full px-1 text-center",
                value === period
                  ? "border-input bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              )}
              onClick={(event) => {
                event.preventDefault()
                onChange(period)
              }}
            >
              {t(periodLabels[period], locale)}
            </button>
          </span>
        )
      })}
    </div>
  )
}

function actionPath(href: string) {
  return href.split("?")[0] || "#"
}

function hiddenFields(href: string) {
  const query = href.split("?")[1]
  if (!query) {
    return []
  }
  return Array.from(new URLSearchParams(query).entries())
}
