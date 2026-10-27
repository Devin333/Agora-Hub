import type { ReactNode } from "react"

export type PageHeaderProps = {
  title: string
  description?: string
  eyebrow?: string
  actions?: ReactNode
}

/**
 * Rakazo full-page header: a hairline-bottomed band with a heading2 title,
 * an optional muted subtitle, and actions on the trailing edge.
 */
export function PageHeader({ title, description, eyebrow, actions }: PageHeaderProps) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-3 border-b border-border pb-4 pt-1">
      <div className="min-w-0">
        {eyebrow ? <p className="mb-1 text-caption font-semibold uppercase tracking-wider text-faint-foreground">{eyebrow}</p> : null}
        <h1 className="text-heading2 font-semibold text-foreground">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-body2 text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}
