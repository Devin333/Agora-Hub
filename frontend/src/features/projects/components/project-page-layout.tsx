import type { ComponentPropsWithoutRef, ReactNode } from "react"
import { ChevronDown, Search } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

/**
 * Rakazo full-page building blocks shared by every Projects surface: a bordered
 * page header, a padded body, hairline panels, card grids, divided lists and
 * plain-text empty states. Pages compose these instead of re-declaring chrome.
 */

/** Card used by every clickable grid item (rakazo artifact card). */
export const projectCardClassName =
  "flex h-full min-w-0 flex-col rounded-2xl border border-border bg-card p-4 text-card-foreground transition-colors hover:bg-accent/40"

/** Link wrapper around a grid card; carries the keyboard focus ring. */
export const projectCardLinkClassName =
  "block min-w-0 rounded-2xl outline-none focus-visible:ring-3 focus-visible:ring-ring/50"

/** Responsive card grid used for project, case, tool and collection directories. */
export const projectGridClassName = "grid gap-3 sm:grid-cols-2 xl:grid-cols-3"

/** Muted sub-surface used inside panels for metric tiles and nested records. */
export const projectInsetClassName = "rounded-lg bg-muted px-3 py-2"

export function ProjectPage({ className, ...props }: ComponentPropsWithoutRef<"main">) {
  return <main className={cn("flex min-w-0 flex-col", className)} {...props} />
}

export function ProjectPageHeader({
  title,
  eyebrow,
  subtitle,
  icon,
  leading,
  actions,
  children,
}: {
  title: ReactNode
  eyebrow?: ReactNode
  subtitle?: ReactNode
  icon?: ReactNode
  leading?: ReactNode
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <header className="border-b border-border px-4 py-4 md:px-6">
      {leading ? <div className="mb-2 flex flex-wrap items-center gap-2">{leading}</div> : null}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex min-w-0 items-center gap-3">
          {icon ? (
            <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-accent-foreground" aria-hidden="true">
              {icon}
            </span>
          ) : null}
          <div className="min-w-0">
            {eyebrow ? <p className="text-label text-muted-foreground">{eyebrow}</p> : null}
            <h1 className="break-words text-heading2 font-semibold text-foreground">{title}</h1>
          </div>
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {subtitle ? <p className="mt-1.5 max-w-3xl break-words text-body2 text-muted-foreground">{subtitle}</p> : null}
      {children ? <div className="mt-3 space-y-3">{children}</div> : null}
    </header>
  )
}

export function ProjectPageBody({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return <div className={cn("min-w-0 space-y-6 px-4 py-5 md:px-6", className)} {...props} />
}

export function ProjectSectionHeading({ title, action, id }: { title: ReactNode; action?: ReactNode; id?: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <h2 id={id} className="text-body1 font-semibold text-foreground">
        {title}
      </h2>
      {action}
    </div>
  )
}

/** Hairline content panel (rakazo settings group surface). */
export function ProjectPanel({
  title,
  eyebrow,
  action,
  className,
  children,
  ...props
}: Omit<ComponentPropsWithoutRef<"section">, "title"> & { title?: ReactNode; eyebrow?: ReactNode; action?: ReactNode }) {
  return (
    <section className={cn("min-w-0 rounded-xl border border-border bg-card p-4 text-card-foreground", className)} {...props}>
      {title || eyebrow || action ? (
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            {eyebrow ? <p className="text-label text-muted-foreground">{eyebrow}</p> : null}
            {title ? <h2 className="break-words text-body1 font-semibold text-foreground">{title}</h2> : null}
          </div>
          {action}
        </div>
      ) : null}
      {children}
    </section>
  )
}

export function ProjectList({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return <div className={cn("flex flex-col divide-y divide-border overflow-hidden rounded-xl border border-border", className)} {...props} />
}

export const projectListRowClassName = "block px-4 py-3 text-body2 transition-colors hover:bg-accent/40"

/** Plain muted empty state: no illustration, no dashed box. */
export function ProjectEmptyText({ title, description, action, className }: { title: ReactNode; description?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("grid place-items-center px-4 py-12 text-center", className)}>
      <div className="max-w-md">
        <p className="text-body2 font-medium text-muted-foreground">{title}</p>
        {description ? <p className="mt-1 text-body2 text-muted-foreground/80">{description}</p> : null}
        {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
      </div>
    </div>
  )
}

/** Compact "label value" pill used for header stats. */
export function ProjectStat({ label, value }: { label: ReactNode; value: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1 text-label text-muted-foreground">
      <span className="font-medium text-foreground">{label}</span>
      <span>{value}</span>
    </span>
  )
}

/** Metric tile (icon + caption + value). */
export function ProjectMetric({ icon, label, value, size = "default" }: { icon?: ReactNode; label: ReactNode; value: ReactNode; size?: "default" | "lg" }) {
  return (
    <div className={cn("min-w-0", projectInsetClassName)}>
      <div className="flex items-center gap-1.5 text-caption uppercase text-muted-foreground">
        {icon}
        <span className="truncate">{label}</span>
      </div>
      <p className={cn("mt-0.5 truncate font-semibold text-foreground", size === "lg" ? "text-heading3" : "text-body2")}>{value}</p>
    </div>
  )
}

/** Pill filter toggle (rakazo FilterChip). */
export function ProjectFilterChip({ active, className, ...props }: ComponentPropsWithoutRef<typeof Button> & { active: boolean }) {
  return <Button type="button" variant={active ? "default" : "outline"} size="sm" aria-pressed={active} className={cn("rounded-full", className)} {...props} />
}

/** Search field with a leading icon (rakazo directory search). */
export function ProjectSearchInput({ className, ...props }: ComponentPropsWithoutRef<typeof Input>) {
  return (
    <div className={cn("relative min-w-0", className)}>
      <Search size={15} strokeWidth={1.8} className="pointer-events-none absolute start-3 top-1/2 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <Input className="ps-9" {...props} />
    </div>
  )
}

/** Native select styled as rakazo NativeSelect; keeps native combobox semantics. */
export function ProjectNativeSelect({ className, wrapperClassName, ...props }: ComponentPropsWithoutRef<"select"> & { wrapperClassName?: string }) {
  return (
    <div className={cn("relative w-fit has-[select:disabled]:opacity-50", wrapperClassName)}>
      <select
        className={cn(
          "h-8 w-full min-w-0 appearance-none rounded-lg border border-input bg-transparent py-1 pe-8 ps-2.5 text-sm text-foreground outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed dark:bg-input/30 dark:hover:bg-input/50",
          className
        )}
        {...props}
      />
      <ChevronDown className="pointer-events-none absolute end-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
    </div>
  )
}

export function ProjectNativeSelectOption({ className, ...props }: ComponentPropsWithoutRef<"option">) {
  return <option className={cn("bg-popover text-popover-foreground", className)} {...props} />
}

/** Fixed-width code block on the muted surface (structured JSON, solutions). */
export function ProjectCodeBlock({ className, ...props }: ComponentPropsWithoutRef<"pre">) {
  return <pre className={cn("rk-scroll max-h-96 overflow-auto rounded-lg bg-muted p-3 font-mono text-label text-foreground", className)} {...props} />
}
