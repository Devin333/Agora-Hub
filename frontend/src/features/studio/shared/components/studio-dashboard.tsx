import type { ComponentType, ReactNode } from "react"
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react"
import { cn } from "@/lib/utils"

type StudioPageHeaderProps = {
  eyebrow?: string
  title: string
  description?: string
  actions?: ReactNode
  meta?: ReactNode
}

export function StudioPageHeader({ eyebrow, title, description, actions, meta }: StudioPageHeaderProps) {
  return (
    <header className="border-b border-border/80 bg-background px-1 pb-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          {eyebrow ? <p className="text-xs font-semibold uppercase tracking-normal text-muted-foreground">{eyebrow}</p> : null}
          <h1 className="mt-1 text-2xl font-semibold tracking-normal text-foreground">{title}</h1>
          {description ? <p className="mt-2 max-w-4xl text-sm leading-6 text-muted-foreground">{description}</p> : null}
          {meta ? <div className="mt-3 flex flex-wrap items-center gap-2">{meta}</div> : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  )
}

type StudioMetricCardProps = {
  label: string
  value: ReactNode
  detail?: ReactNode
  icon?: ComponentType<{ className?: string }>
  tone?: "neutral" | "success" | "warning" | "danger" | "info" | "accent"
}

export function StudioMetricGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-6", className)}>{children}</section>
}

export function StudioMetricCard({ label, value, detail, icon: Icon, tone = "neutral" }: StudioMetricCardProps) {
  const toneClass = {
    neutral: "text-foreground",
    success: "text-success",
    warning: "text-warning",
    danger: "text-destructive",
    info: "text-link",
    accent: "text-link"
  }[tone]

  return (
    <article className="rounded-md border border-border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <p className="truncate text-xs font-semibold uppercase tracking-normal text-muted-foreground">{label}</p>
        {Icon ? <Icon className={cn("size-4 shrink-0", toneClass)} /> : null}
      </div>
      <div className={cn("mt-3 truncate text-2xl font-semibold tracking-normal", toneClass)}>{value}</div>
      {detail ? <p className="mt-1 truncate text-xs text-muted-foreground">{detail}</p> : null}
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
}

export function StudioPanel({ title, description, actions, children, className, contentClassName }: StudioPanelProps) {
  return (
    <section className={cn("rounded-md border border-border bg-card shadow-sm", className)}>
      {(title || description || actions) ? (
        <div className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            {title ? <h2 className="truncate text-sm font-semibold text-foreground">{title}</h2> : null}
            {description ? <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className={cn("p-4", contentClassName)}>{children}</div>
    </section>
  )
}

export function StudioFieldGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4", className)}>{children}</div>
}

export function StudioField({ label, value }: { label: string; value?: ReactNode }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-secondary/30 px-3 py-2">
      <p className="text-xs font-semibold uppercase tracking-normal text-muted-foreground">{label}</p>
      <div className="mt-1 min-w-0 break-words text-sm font-medium text-foreground">{value ?? "n/a"}</div>
    </div>
  )
}

export function StudioToolbar({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-md border border-border bg-card p-3 shadow-sm", className)}>
      {children}
    </section>
  )
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
    warning: "border-warning/30 bg-warning/10 text-warning",
    info: "border-link/30 bg-link/10 text-link",
    success: "border-success/30 bg-success/10 text-success",
    danger: "border-destructive/30 bg-destructive/10 text-destructive"
  }[tone]
  const Icon = tone === "success" ? CheckCircle2 : tone === "danger" ? XCircle : tone === "info" ? Info : AlertTriangle

  return (
    <section className={cn("rounded-md border px-4 py-3 text-sm shadow-sm", toneClass)}>
      <div className="flex items-start gap-3">
        <Icon className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0">
          {title ? <p className="font-semibold">{title}</p> : null}
          <div className={cn(title && "mt-1", "leading-6 text-foreground")}>{children}</div>
        </div>
      </div>
    </section>
  )
}

export function StudioTableFrame({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cn("overflow-hidden rounded-md border border-border bg-card shadow-sm", className)}>{children}</section>
}

export function StudioEmptyBlock({ title, description }: { title: string; description?: string }) {
  return (
    <div className="rounded-md border border-dashed border-border bg-secondary/30 px-5 py-8 text-center">
      <p className="text-sm font-semibold text-foreground">{title}</p>
      {description ? <p className="mt-2 text-sm leading-6 text-muted-foreground">{description}</p> : null}
    </div>
  )
}
