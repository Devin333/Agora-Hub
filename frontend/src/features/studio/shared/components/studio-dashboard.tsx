import type { ComponentType, ReactNode } from "react"
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react"
import { TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"

/** Lucide-compatible icon component accepted by Studio primitives. */
export type StudioIcon = ComponentType<{ className?: string; size?: number; strokeWidth?: number; "aria-hidden"?: boolean | "true" | "false" }>

type StudioPageHeaderProps = {
  eyebrow?: string
  title: string
  description?: string
  actions?: ReactNode
  meta?: ReactNode
}

/**
 * Rakazo full-page header band for Studio pages. Mirrors the shared `PageHeader`
 * recipe and adds an optional `meta` row for status badges and identifiers.
 */
export function StudioPageHeader({ eyebrow, title, description, actions, meta }: StudioPageHeaderProps) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-3 border-b border-border pb-4 pt-1">
      <div className="min-w-0">
        {eyebrow ? <p className="mb-1 text-caption font-semibold uppercase tracking-wider text-faint-foreground">{eyebrow}</p> : null}
        <h1 className="break-words text-heading2 font-semibold text-foreground">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-body2 text-muted-foreground">{description}</p> : null}
        {meta ? <div className="mt-3 flex flex-wrap items-center gap-2">{meta}</div> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}

export type StudioTone = "neutral" | "success" | "warning" | "danger" | "info" | "accent"

/** Readable token text color for a Studio status tone (values, labels). */
export const studioToneTextClass: Record<StudioTone, string> = {
  neutral: "text-foreground",
  success: "text-success-text",
  warning: "text-warning-text",
  danger: "text-destructive-text",
  info: "text-link",
  accent: "text-status-working"
}

/** Raw token hue for a Studio status tone (icons and dots only). */
export const studioToneIconClass: Record<StudioTone, string> = {
  neutral: "text-muted-foreground",
  success: "text-success",
  warning: "text-warning",
  danger: "text-destructive",
  info: "text-link",
  accent: "text-status-working"
}

type StudioMetricCardProps = {
  label: string
  value: ReactNode
  detail?: ReactNode
  icon?: StudioIcon
  tone?: StudioTone
}

export function StudioMetricGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-6", className)}>{children}</section>
}

/** Rakazo stat tile: hairline `rounded-2xl` surface, caption label, heading-sized value. */
export function StudioMetricCard({ label, value, detail, icon: Icon, tone = "neutral" }: StudioMetricCardProps) {
  const toneClass = studioToneTextClass[tone]
  const iconClass = studioToneIconClass[tone]

  return (
    <article className="min-w-0 rounded-2xl border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <p className="truncate text-label text-muted-foreground">{label}</p>
        {Icon ? <Icon size={16} strokeWidth={1.8} aria-hidden="true" className={cn("shrink-0", iconClass)} /> : null}
      </div>
      <div className={`mt-2 truncate text-heading2 font-semibold tabular-nums ${toneClass}`}>{value}</div>
      {detail ? <p className="mt-1 truncate text-label text-muted-foreground">{detail}</p> : null}
    </article>
  )
}

type StudioPanelProps = {
  title?: string
  description?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  contentClassName?: string
  /** Render the body edge-to-edge below a hairline, for divided lists and tables. */
  flush?: boolean
}

/** Framed Studio section (`rounded-2xl border bg-card`) with a 15px title row. */
export function StudioPanel({ title, description, actions, children, className, contentClassName, flush = false }: StudioPanelProps) {
  const hasHeader = Boolean(title || description || actions)

  return (
    <section className={cn("min-w-0 overflow-hidden rounded-2xl border border-border bg-card", className)}>
      {hasHeader ? (
        <div className="flex flex-wrap items-start justify-between gap-2 px-4 pb-3 pt-4">
          <div className="min-w-0">
            {title ? <h2 className="truncate text-[15px] font-semibold text-foreground">{title}</h2> : null}
            {description ? <p className="mt-0.5 text-label text-muted-foreground">{description}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className={cn(flush ? "border-t border-border" : hasHeader ? "px-4 pb-4" : "p-4", contentClassName)}>{children}</div>
    </section>
  )
}

export function StudioFieldGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("grid gap-2 text-body2 sm:grid-cols-2 xl:grid-cols-4", className)}>{children}</div>
}

export function StudioField({ label, value }: { label: string; value?: ReactNode }) {
  return (
    <div className="min-w-0 rounded-xl border border-border px-3 py-2.5">
      <p className="text-label text-muted-foreground">{label}</p>
      <div className="mt-0.5 min-w-0 break-words text-[13.5px] font-medium text-foreground">{value ?? "n/a"}</div>
    </div>
  )
}

/** Unframed filter row that sits directly below the page header (rakazo list pages). */
export function StudioToolbar({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cn("min-w-0", className)}>{children}</section>
}

export function StudioNotice({
  title,
  children,
  tone = "warning"
}: {
  title?: string
  children: ReactNode
  tone?: "warning" | "info" | "success" | "danger"
}) {
  const toneClass = {
    warning: { frame: "border-warning/40 bg-warning/10", title: "text-warning-text", icon: "text-warning" },
    info: { frame: "border-link/40 bg-link/10", title: "text-link", icon: "text-link" },
    success: { frame: "border-success/40 bg-success/10", title: "text-success-text", icon: "text-success" },
    danger: { frame: "border-destructive/30 bg-destructive/10", title: "text-destructive-text", icon: "text-destructive" }
  }[tone]
  const Icon = tone === "success" ? CheckCircle2 : tone === "danger" ? XCircle : tone === "info" ? Info : AlertTriangle

  return (
    <section className={cn("rounded-xl border px-4 py-3 text-body2", toneClass.frame)}>
      <div className="flex items-start gap-3">
        <Icon size={16} strokeWidth={1.8} aria-hidden="true" className={cn("mt-0.5 shrink-0", toneClass.icon)} />
        <div className="min-w-0">
          {title ? <p className={cn("font-medium", toneClass.title)}>{title}</p> : null}
          <div className={cn(title && "mt-1", "text-foreground/80")}>{children}</div>
        </div>
      </div>
    </section>
  )
}

/**
 * Rakazo text-field recipe for raw `<input>` / `<textarea>` controls in Studio
 * forms. It carries no font size: callers append height, padding and exactly one
 * size (`text-[13px]` or `text-label`) via template strings, never `cn()`, because
 * `cn` would treat a token size and `text-foreground` as conflicting colors.
 */
export const studioFieldClass =
  "w-full rounded-lg border border-input bg-transparent px-3 text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"

/**
 * Header cell recipe for `components/ui/table` inside Studio table frames. The
 * caption size lives on the `<thead>` (see `StudioTableHeader`) so it never shares
 * a `cn()` merge with the color class.
 */
export const studioTableHeadClass = "h-9 px-4 font-medium text-muted-foreground"

/** `<thead>` with the rakazo caption size for Studio tables. */
export function StudioTableHeader({ columns, className }: { columns: ReactNode[]; className?: string }) {
  return (
    <TableHeader className="text-label">
      <TableRow className="hover:bg-transparent">
        {columns.map((column, index) => (
          <TableHead key={index} className={cn(studioTableHeadClass, className)}>
            {column}
          </TableHead>
        ))}
      </TableRow>
    </TableHeader>
  )
}

/** Body cell recipe matching rakazo list rows (`px-4 py-3`). */
export const studioTableCellClass = "px-4 py-3 align-middle"

/** Hairline frame for data tables (`rounded-xl border`). */
export function StudioTableFrame({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cn("overflow-hidden rounded-xl border border-border bg-card", className)}>{children}</section>
}

/** Rakazo empty state: plain muted copy, no illustration or dashed box. */
export function StudioEmptyBlock({ title, description }: { title: string; description?: string }) {
  return (
    <div className="px-4 py-8 text-center">
      <p className="text-body2 font-medium text-muted-foreground">{title}</p>
      {description ? <p className="mx-auto mt-1 max-w-xl text-label text-muted-foreground/80">{description}</p> : null}
    </div>
  )
}

/** Divided list frame (`divide-y rounded-xl border`) used for row lists outside panels. */
export function StudioList({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("divide-y divide-border overflow-hidden rounded-xl border border-border", className)}>{children}</div>
}

/**
 * Monospace code/JSON block with a thin scroll pane. The font size is appended
 * outside `cn()` so caller color overrides cannot merge it away.
 */
export function StudioCodeBlock({ children, className }: { children: ReactNode; className?: string }) {
  return <pre className={`${cn("rk-scroll overflow-auto whitespace-pre-wrap break-words rounded-xl bg-muted p-3 font-mono text-foreground/90", className)} text-label`}>{children}</pre>
}
