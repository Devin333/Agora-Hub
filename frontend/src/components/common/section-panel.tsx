import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/**
 * Titled content section in the rakazo full-page idiom. `framed` wraps the body
 * in a hairline `rounded-2xl` surface; unframed sections sit directly on the page.
 */
export function SectionPanel({
  title,
  description,
  actions,
  framed = true,
  className,
  bodyClassName,
  children,
  headingLevel = 2
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  framed?: boolean
  className?: string
  bodyClassName?: string
  children: ReactNode
  headingLevel?: 2 | 3
}) {
  const Heading = headingLevel === 2 ? "h2" : "h3"
  return (
    <section className={cn(framed && "rounded-2xl border border-border bg-card p-4", className)}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Heading className="text-[15px] font-semibold text-foreground">{title}</Heading>
          {description ? <p className="mt-0.5 text-label text-muted-foreground">{description}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
      <div className={bodyClassName}>{children}</div>
    </section>
  )
}

/** Caption-style group label used above filter groups and sidebar sections. */
export function SectionCaption({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("px-0.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground", className)}>{children}</p>
}
