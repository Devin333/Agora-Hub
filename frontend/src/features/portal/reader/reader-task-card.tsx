import type { ReactNode } from "react"
import type { LucideIcon } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * Shared frame for reader-side task controls (source preparation, research analysis).
 * Pure presentation: callers own every state transition and pass the rendered actions in.
 */
export function ReaderTaskCard({
  icon: Icon,
  title,
  description,
  warning,
  error,
  className,
  children,
}: {
  icon: LucideIcon
  title: string
  description: string
  warning?: ReactNode
  error?: string
  className?: string
  children?: ReactNode
}) {
  return (
    <section
      className={cn(
        "shrink-0 rounded-xl bg-card px-4 py-3 text-body2 text-card-foreground ring-1 ring-foreground/10",
        className,
      )}
    >
      <strong className="inline-flex items-center gap-2 text-body2 font-semibold text-foreground">
        <Icon size={15} strokeWidth={1.8} className="text-muted-foreground" aria-hidden="true" />
        {title}
      </strong>
      <p className="mt-1 text-label text-muted-foreground">{description}</p>
      {warning && (
        <p className="mt-2 flex items-start gap-2 rounded-lg bg-warning/15 px-2.5 py-1.5 text-label text-warning-text">
          <span className="mt-[5px] size-1.5 shrink-0 rounded-full bg-warning" aria-hidden="true" />
          <span className="min-w-0">{warning}</span>
        </p>
      )}
      {children && <div className="mt-2.5 flex flex-wrap items-center gap-2">{children}</div>}
      {error && <p role="alert" className="mt-2 text-label text-destructive-text">{error}</p>}
    </section>
  )
}

/** Live operation status line rendered inside a task card. */
export function ReaderTaskStatus({ children }: { children: ReactNode }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-label text-muted-foreground">
      <span className="size-1.5 rounded-full bg-status-working" aria-hidden="true" />
      {children}
    </span>
  )
}
