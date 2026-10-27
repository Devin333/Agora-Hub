import type { ReactNode } from "react"

export type EmptyStateProps = {
  title: string
  description?: string
  action?: ReactNode
}

/** Rakazo empty state: plain muted copy centred in the pane, no illustration. */
export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div className="grid min-h-40 place-items-center px-6 py-10 text-center">
      <div className="flex flex-col items-center">
        <p className="text-body2 font-medium text-muted-foreground">{title}</p>
        {description ? <p className="mt-1 max-w-md text-body2 text-muted-foreground/80">{description}</p> : null}
        {action ? <div className="mt-4">{action}</div> : null}
      </div>
    </div>
  )
}
