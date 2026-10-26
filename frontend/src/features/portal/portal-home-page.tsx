import Link from "next/link"
import { ArrowRight, CircleCheck, CircleDashed, CircleAlert, Network, Newspaper, Radio, FileText, GitBranch, BookOpen } from "lucide-react"
import { cn } from "@/lib/utils"
import type { PortalHomeData, PortalModuleStatus, PortalModuleSummary, PortalResearchEntry } from "@/features/portal/portal-home-data"

const moduleIcons: Record<PortalModuleSummary["id"], typeof Newspaper> = {
  news: Newspaper,
  projects: GitBranch,
  papers: BookOpen,
  community: Radio,
  evidence: Network,
  reports: FileText
}

export function PortalHomePage({ data }: { data: PortalHomeData }) {
  const primary = data.modules[0]
  const secondary = data.modules.slice(1)

  return (
    <div className="space-y-10">
      <section className="grid gap-8 py-10 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-end">
        <div className="min-w-0">
          <p className="mb-5 text-xs font-medium uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">
            Agora Hub Portal
          </p>
          <h1 className="max-w-5xl break-keep text-5xl font-black leading-none tracking-normal text-[#334155] sm:text-6xl lg:text-7xl dark:text-foreground">
            AI intelligence{" "}
            <span className="bg-gradient-to-r from-emerald-600 via-teal-600 to-blue-600 bg-clip-text text-transparent">
              front page
            </span>
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-7 text-[#334155]/70 dark:text-muted-foreground">
            A unified entry for AI news, research papers, open-source projects, community signals, evidence chains, and briefings.
          </p>
          <div className="mt-7 flex flex-wrap gap-2">
            <PortalPill label="Public homepage" value="/" />
            <PortalPill label="Ready modules" value={`${data.readyModules}/${data.modules.length}`} />
            <PortalPill label="Signals" value={data.totalSignals} />
          </div>
        </div>

        <div className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-[0_24px_60px_rgba(15,23,42,0.10)] dark:border-border dark:bg-card">
          <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">Current lead</p>
          {primary ? <LeadModule module={primary} /> : null}
        </div>
      </section>

      <section aria-label="Portal modules" className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {data.modules.map((module) => (
          <PortalModuleCard key={module.id} module={module} />
        ))}
      </section>

      <section aria-label="Paper Radar research entries" className="border-t border-[#d7dfd8] pt-8 dark:border-border">
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Research Module</p>
            <h2 className="mt-2 text-2xl font-black text-[#334155] dark:text-foreground">Paper Radar</h2>
          </div>
          <Link href="/papers" className="inline-flex items-center gap-2 text-sm font-semibold text-emerald-700 hover:text-emerald-800">
            Open research board
            <ArrowRight className="size-4" />
          </Link>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          {data.researchEntries.map((entry) => (
            <ResearchEntryCard key={entry.href} entry={entry} />
          ))}
        </div>
      </section>

      <section className="grid gap-8 border-t border-[#d7dfd8] pt-8 xl:grid-cols-[16rem_minmax(0,1fr)] dark:border-border">
        <aside className="space-y-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Boards</p>
          <div className="space-y-2">
            {data.modules.map((module) => (
              <Link
                key={module.id}
                href={module.href}
                className="flex items-center justify-between rounded-md border border-[#dbe3dc] bg-white/70 px-3 py-2 text-sm text-[#334155] transition-colors hover:bg-white dark:border-border dark:bg-card dark:text-foreground"
              >
                <span>{module.title}</span>
                <span className={cn("size-2 rounded-full", statusDotClass(module.status))} />
              </Link>
            ))}
          </div>
        </aside>

        <div className="grid gap-3">
          {secondary.flatMap((module) =>
            module.highlights.length
              ? module.highlights.slice(0, 2).map((highlight) => (
                  <Link
                    key={`${module.id}-${highlight.title}`}
                    href={highlight.href ?? module.href}
                    className="grid gap-2 rounded-md border border-[#dbe3dc] bg-white/75 p-4 transition-colors hover:bg-white md:grid-cols-[10rem_minmax(0,1fr)_auto] md:items-center dark:border-border dark:bg-card"
                  >
                    <span className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">
                      {module.title}
                    </span>
                    <span className="min-w-0 text-sm font-medium text-[#334155] dark:text-foreground">{highlight.title}</span>
                    <span className="text-xs text-[#334155]/55 dark:text-muted-foreground">{highlight.meta}</span>
                  </Link>
                ))
              : [
                  <div
                    key={`${module.id}-empty`}
                    className="grid gap-2 rounded-md border border-dashed border-[#dbe3dc] bg-white/50 p-4 md:grid-cols-[10rem_minmax(0,1fr)] dark:border-border dark:bg-card/70"
                  >
                    <span className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">
                      {module.title}
                    </span>
                    <span className="text-sm text-[#334155]/60 dark:text-muted-foreground">
                      {module.notices[0] ?? "No current items available from this source."}
                    </span>
                  </div>
                ]
          )}
        </div>
      </section>
    </div>
  )
}

function ResearchEntryCard({ entry }: { entry: PortalResearchEntry }) {
  const idBase = `research-entry-${entry.href.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "") || "root"}`
  const titleId = `${idBase}-title`
  const descriptionId = `${idBase}-description`

  return (
    <Link
      href={entry.href}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      className="group rounded-md border border-[#dbe3dc] bg-white/80 p-4 transition-colors hover:bg-white dark:border-border dark:bg-card"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id={titleId} className="text-base font-semibold text-[#334155] dark:text-foreground">{entry.title}</h3>
          <p id={descriptionId} className="mt-2 line-clamp-3 text-sm leading-6 text-[#334155]/65 dark:text-muted-foreground">{entry.description}</p>
        </div>
        <ArrowRight className="size-4 shrink-0 text-[#334155]/45 transition-transform group-hover:translate-x-0.5 dark:text-muted-foreground" />
      </div>
      <div className="mt-4 rounded-md border border-[#edf1ed] bg-[#f7f9f6] px-3 py-2 dark:border-border dark:bg-background">
        <p className="text-[11px] uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">{entry.metricLabel}</p>
        <p className="mt-1 text-sm font-semibold text-[#334155] dark:text-foreground">{entry.metricValue}</p>
      </div>
      {entry.highlights.length ? (
        <p className="mt-3 line-clamp-1 text-xs text-[#334155]/55 dark:text-muted-foreground">{entry.highlights[0].title}</p>
      ) : null}
    </Link>
  )
}

function LeadModule({ module }: { module: PortalModuleSummary }) {
  const Icon = moduleIcons[module.id]
  return (
    <div className="mt-4 space-y-4">
      <div className="flex items-start gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-[#0f172a] text-white">
          <Icon className="size-5" />
        </span>
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-[#334155] dark:text-foreground">{module.title}</h2>
          <p className="mt-1 text-sm leading-6 text-[#334155]/65 dark:text-muted-foreground">{module.description}</p>
        </div>
      </div>
      <Link href={module.href} className="inline-flex items-center gap-2 text-sm font-semibold text-emerald-700 hover:text-emerald-800">
        Open board
        <ArrowRight className="size-4" />
      </Link>
    </div>
  )
}

function PortalModuleCard({ module }: { module: PortalModuleSummary }) {
  const Icon = moduleIcons[module.id]
  return (
    <article className="rounded-md border border-[#dbe3dc] bg-white/85 p-5 shadow-sm dark:border-border dark:bg-card">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-[#eef3ef] text-[#334155] dark:bg-secondary dark:text-foreground">
            <Icon className="size-5" />
          </span>
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">{module.eyebrow}</p>
            <h2 className="mt-1 text-lg font-semibold text-[#334155] dark:text-foreground">{module.title}</h2>
          </div>
        </div>
        <StatusBadge status={module.status} />
      </div>

      <p className="mt-4 min-h-16 text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">{module.description}</p>

      <div className="mt-5 grid grid-cols-3 gap-2">
        {module.metrics.map((metric) => (
          <div key={metric.label} className="rounded-md border border-[#edf1ed] bg-[#f7f9f6] px-3 py-2 dark:border-border dark:bg-background">
            <p className="text-[11px] uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">{metric.label}</p>
            <p className="mt-1 truncate text-sm font-semibold text-[#334155] dark:text-foreground">{metric.value}</p>
          </div>
        ))}
      </div>

      <div className="mt-5 space-y-2">
        {module.highlights.length ? (
          module.highlights.map((highlight) => (
            <Link
              key={highlight.title}
              href={highlight.href ?? module.href}
              className="block rounded-md border border-transparent px-2 py-1.5 text-sm text-[#334155] transition-colors hover:border-[#dbe3dc] hover:bg-[#f7f9f6] dark:text-foreground dark:hover:border-border dark:hover:bg-background"
            >
              <span className="line-clamp-1 font-medium">{highlight.title}</span>
              {highlight.meta ? <span className="mt-1 block truncate text-xs text-[#334155]/55 dark:text-muted-foreground">{highlight.meta}</span> : null}
            </Link>
          ))
        ) : (
          <p className="rounded-md border border-dashed border-[#dbe3dc] px-3 py-3 text-sm text-[#334155]/60 dark:border-border dark:text-muted-foreground">
            {module.notices[0] ?? "No current items available."}
          </p>
        )}
      </div>

      <Link href={module.href} className="mt-5 inline-flex items-center gap-2 text-sm font-semibold text-emerald-700 hover:text-emerald-800">
        View {module.title}
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

function PortalPill({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-[#dbe5dd] bg-white/90 px-3 py-1 text-xs text-[#334155]/70 shadow-sm dark:border-border dark:bg-card dark:text-muted-foreground">
      <span className="font-medium text-[#334155] dark:text-foreground">{label}</span>
      {value}
    </span>
  )
}

function statusBadgeClass(status: PortalModuleStatus) {
  if (status === "ready") return "border-emerald-200 bg-emerald-50 text-emerald-800"
  if (status === "empty") return "border-slate-200 bg-slate-50 text-slate-700"
  return "border-amber-200 bg-amber-50 text-amber-800"
}

function statusDotClass(status: PortalModuleStatus) {
  if (status === "ready") return "bg-emerald-500"
  if (status === "empty") return "bg-slate-400"
  return "bg-amber-500"
}
