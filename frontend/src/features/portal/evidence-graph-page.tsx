import Link from "next/link"
import { ArrowRight, CircleAlert, CircleCheck, CircleDashed, Network } from "lucide-react"
import { cn } from "@/lib/utils"
import type { EvidenceGraphData, EvidenceGraphSection, PortalModuleStatus } from "@/features/portal/portal-home-data"

export function EvidenceGraphPage({ data }: { data: EvidenceGraphData }) {
  const total = data.sections.reduce((count, section) => count + section.count, 0)

  return (
    <div className="space-y-8">
      <section className="grid gap-8 py-8 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-end">
        <div>
          <p className="mb-5 text-xs font-medium uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">
            Trends / Evidence
          </p>
          <h1 className="max-w-5xl break-keep text-5xl font-black leading-none tracking-normal text-[#334155] sm:text-6xl lg:text-7xl dark:text-foreground">
            Cross-board{" "}
            <span className="bg-gradient-to-r from-emerald-600 via-teal-600 to-blue-600 bg-clip-text text-transparent">
              Evidence Graph
            </span>
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-7 text-[#334155]/70 dark:text-muted-foreground">{data.summary}</p>
        </div>
        <div className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-[0_24px_60px_rgba(15,23,42,0.10)] dark:border-border dark:bg-card">
          <div className="flex items-center gap-3">
            <span className="flex size-10 items-center justify-center rounded-md bg-[#0f172a] text-white">
              <Network className="size-5" />
            </span>
            <div>
              <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">Graph scope</p>
              <p className="text-2xl font-semibold text-[#334155] dark:text-foreground">{total}</p>
            </div>
          </div>
          <p className="mt-4 text-sm leading-6 text-[#334155]/65 dark:text-muted-foreground">
            Paper, project, news, and community evidence are shown as structured sections in this MVP view.
          </p>
        </div>
      </section>

      {data.notices.length ? (
        <section className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
          {data.notices.slice(0, 3).join(" ")}
        </section>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-4">
        {data.sections.map((section) => (
          <EvidenceSectionCard key={section.title} section={section} />
        ))}
      </section>

      <section className="grid gap-8 border-t border-[#d7dfd8] pt-8 xl:grid-cols-[16rem_minmax(0,1fr)] dark:border-border">
        <aside>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Signal mix</p>
          <div className="mt-3 space-y-2">
            {data.sections.map((section) => (
              <Link
                key={section.title}
                href={section.href}
                className="flex items-center justify-between rounded-md border border-[#dbe3dc] bg-white/70 px-3 py-2 text-sm text-[#334155] transition-colors hover:bg-white dark:border-border dark:bg-card dark:text-foreground"
              >
                <span>{section.title}</span>
                <span>{section.count}</span>
              </Link>
            ))}
          </div>
        </aside>

        <div className="space-y-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Timeline</p>
          {data.timeline.length ? (
            data.timeline.map((item) => (
              <Link
                key={item.title}
                href={item.href ?? "/topics?view=evidence-graph"}
                className="grid gap-2 rounded-md border border-[#dbe3dc] bg-white/75 p-4 transition-colors hover:bg-white md:grid-cols-[minmax(0,1fr)_8rem] md:items-center dark:border-border dark:bg-card"
              >
                <span className="font-medium text-[#334155] dark:text-foreground">{item.title}</span>
                <span className="text-xs text-[#334155]/55 dark:text-muted-foreground">{item.meta}</span>
              </Link>
            ))
          ) : (
            <div className="rounded-md border border-dashed border-[#dbe3dc] bg-white/50 p-4 text-sm text-[#334155]/60 dark:border-border dark:bg-card dark:text-muted-foreground">
              No evidence timeline is available from the current data sources.
            </div>
          )}
        </div>
      </section>
    </div>
  )
}

function EvidenceSectionCard({ section }: { section: EvidenceGraphSection }) {
  return (
    <article className="rounded-md border border-[#dbe3dc] bg-white/85 p-5 shadow-sm dark:border-border dark:bg-card">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">Evidence</p>
          <h2 className="mt-1 text-lg font-semibold text-[#334155] dark:text-foreground">{section.title}</h2>
        </div>
        <StatusBadge status={section.status} />
      </div>
      <p className="mt-4 text-3xl font-semibold text-[#334155] dark:text-foreground">{section.count}</p>
      <div className="mt-4 space-y-2">
        {section.items.length ? (
          section.items.slice(0, 3).map((item) => (
            <Link
              key={item.title}
              href={item.href ?? section.href}
              className="block rounded-md border border-transparent px-2 py-1.5 text-sm text-[#334155] transition-colors hover:border-[#dbe3dc] hover:bg-[#f7f9f6] dark:text-foreground dark:hover:border-border dark:hover:bg-background"
            >
              <span className="line-clamp-1 font-medium">{item.title}</span>
              {item.meta ? <span className="mt-1 block truncate text-xs text-[#334155]/55 dark:text-muted-foreground">{item.meta}</span> : null}
            </Link>
          ))
        ) : (
          <p className="rounded-md border border-dashed border-[#dbe3dc] px-3 py-3 text-sm text-[#334155]/60 dark:border-border dark:text-muted-foreground">
            No {section.title.toLowerCase()} evidence is available.
          </p>
        )}
      </div>
      <Link href={section.href} className="mt-5 inline-flex items-center gap-2 text-sm font-semibold text-emerald-700 hover:text-emerald-800">
        Open source view
        <ArrowRight className="size-4" />
      </Link>
    </article>
  )
}

function StatusBadge({ status }: { status: PortalModuleStatus }) {
  const Icon = status === "ready" ? CircleCheck : status === "empty" ? CircleDashed : CircleAlert
  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-1 text-xs", statusBadgeClass(status))}>
      <Icon className="size-3" />
      {status}
    </span>
  )
}

function statusBadgeClass(status: PortalModuleStatus) {
  if (status === "ready") return "border-emerald-200 bg-emerald-50 text-emerald-800"
  if (status === "empty") return "border-slate-200 bg-slate-50 text-slate-700"
  return "border-amber-200 bg-amber-50 text-amber-800"
}
