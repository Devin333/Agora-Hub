import Link from "next/link"
import type { ReactNode } from "react"
import { ArrowRight } from "lucide-react"
import { cn } from "@/lib/utils"

export type RelatedListItem = {
  id: string
  label: string
  /** Secondary line under the label (category, paper title...). */
  detail?: string
  /** Trailing value, e.g. a paper count. */
  count?: ReactNode
} & ({ href: string; onSelect?: never } | { href?: never; onSelect: () => void })

const rowClassName =
  "flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-body2 text-foreground outline-none transition-colors hover:bg-accent/40 focus-visible:bg-accent/40"

/**
 * Rakazo list section for paper taxonomy relations: a small uppercase heading
 * above a `divide-y rounded-xl border` list of link or button rows.
 */
export function RelatedListPanel({ title, items, className }: { title: string; items: RelatedListItem[]; className?: string }) {
  if (!items.length) return null
  return (
    <section className={cn("space-y-2", className)}>
      <h2 className="px-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{title}</h2>
      <ul className="divide-y overflow-hidden rounded-xl border">
        {items.map((item) => (
          <li key={item.id}>
            {item.href ? (
              <Link href={item.href} className={rowClassName}>
                <RowContent item={item} />
              </Link>
            ) : (
              <button type="button" className={rowClassName} onClick={item.onSelect}>
                <RowContent item={item} />
                <ArrowRight className="size-4 shrink-0 text-faint-foreground" strokeWidth={1.8} aria-hidden="true" />
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

function RowContent({ item }: { item: RelatedListItem }) {
  return (
    <>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{item.label}</span>
        {item.detail ? <span className="mt-0.5 block truncate text-label text-muted-foreground">{item.detail}</span> : null}
      </span>
      {item.count !== undefined ? <span className="shrink-0 text-label tabular-nums text-faint-foreground">{item.count}</span> : null}
    </>
  )
}
