import Link from "next/link"
import { ChevronRight } from "lucide-react"
import { papersCopy, t } from "@/lib/papers/copy"
import type { Locale } from "@/lib/papers/types"

export function PapersMicrobar({
  items,
  meta,
  locale
}: {
  items: Array<{ label: string; href?: string }>
  meta: string
  locale: Locale
}) {
  return (
    <div className="flex flex-col gap-2 px-4 pb-1 pt-4 sm:flex-row sm:items-center sm:justify-between md:px-6">
      <nav className="flex flex-wrap items-center gap-1.5 text-label text-muted-foreground" aria-label="Research breadcrumb">
        <Link href="/papers" className="rounded-sm font-medium outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
          Research
        </Link>
        {items.map((item) => (
          <span key={`${item.label}-${item.href ?? "current"}`} className="inline-flex items-center gap-1.5">
            <ChevronRight className="size-3 text-faint-foreground" strokeWidth={1.8} aria-hidden="true" />
            {item.href ? (
              <Link href={item.href} className="rounded-sm outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                {item.label}
              </Link>
            ) : (
              <span className="text-foreground">{item.label}</span>
            )}
          </span>
        ))}
      </nav>
      <span className="inline-flex h-6 w-fit items-center gap-1.5 rounded-full border px-2.5 text-label text-muted-foreground">
        <span className="size-1.5 rounded-full bg-success" aria-hidden="true" />
        {meta || t(papersCopy.frontendView, locale)}
      </span>
    </div>
  )
}
