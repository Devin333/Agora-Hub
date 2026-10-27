import type { ReactNode } from "react"
import { SummaryCard } from "@/components/papers/summary-card"

/**
 * Rakazo full-page header: a bordered band with the heading2 title, a muted
 * subtitle and, on the right, the page's stat tiles plus optional controls.
 * `variant` is kept for callers; both render the same bordered header.
 */
export function PapersHero({
  eyebrow,
  title,
  subtitle,
  stats,
  aside
}: {
  eyebrow?: string
  title: string
  subtitle: string
  stats: Array<{ label: string; value: string | number }>
  aside?: ReactNode
  variant?: "default" | "editorial"
}) {
  return (
    <header className="grid gap-4 border-b px-4 py-4 md:px-6 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)] lg:items-end">
      <div className="min-w-0">
        {eyebrow ? <p className="mb-1 text-label font-medium text-muted-foreground">{eyebrow}</p> : null}
        <h1 className="break-keep text-heading2 font-semibold text-foreground">{title}</h1>
        <p className="mt-1 max-w-2xl text-body2 text-muted-foreground">{subtitle}</p>
      </div>
      {stats.length || aside ? (
        <div className="w-full space-y-2.5 lg:justify-self-end">
          {stats.length ? (
            <div className="grid grid-cols-3 gap-2">
              {stats.map((stat) => (
                <SummaryCard key={stat.label} label={stat.label} value={stat.value} compact />
              ))}
            </div>
          ) : null}
          {aside ? <div className="w-full">{aside}</div> : null}
        </div>
      ) : null}
    </header>
  )
}
