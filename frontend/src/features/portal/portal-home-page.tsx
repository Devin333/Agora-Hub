import Link from "next/link"
import { ArrowRight, BookOpen, CircleAlert, CircleCheck, CircleDashed, FileText, GitBranch, Network, Newspaper, Radio } from "lucide-react"
import { SectionCaption } from "@/components/common/section-panel"
import { PageHeader } from "@/components/layout/page-header"
import { Badge, type BadgeVariant } from "@/components/ui/badge"
import type { PortalHomeData, PortalModuleStatus, PortalModuleSummary, PortalResearchEntry } from "@/features/portal/portal-home-data"

const moduleIcons: Record<PortalModuleSummary["id"], typeof Newspaper> = {
  news: Newspaper,
  projects: GitBranch,
  papers: BookOpen,
  community: Radio,
  evidence: Network,
  reports: FileText
}

const statusVariant: Record<PortalModuleStatus, BadgeVariant> = {
  ready: "success",
  empty: "muted",
  degraded: "warning"
}

const statusDotClass: Record<PortalModuleStatus, string> = {
  ready: "bg-success",
  empty: "bg-faint-foreground",
  degraded: "bg-warning"
}

const statusIcon = {
  ready: CircleCheck,
  empty: CircleDashed,
  degraded: CircleAlert
} satisfies Record<PortalModuleStatus, typeof CircleCheck>

const textLinkClass =
  "inline-flex items-center gap-1.5 rounded-sm text-body2 font-medium text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"

const cardLinkClass =
  "rounded-2xl border border-border bg-card p-4 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50"

export function PortalHomePage({ data }: { data: PortalHomeData }) {
  const primary = data.modules[0]
  const secondary = data.modules.slice(1)

  return (
    <div className="space-y-8">
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-end">
        <PageHeader
          eyebrow="Agora Hub Portal"
          title="AI intelligence front page"
          description="A unified entry for AI news, research papers, open-source projects, community signals, evidence chains, and briefings."
          actions={
            <dl className="flex flex-wrap gap-1.5">
              <PortalPill label="Public homepage" value="/" />
              <PortalPill label="Ready modules" value={`${data.readyModules}/${data.modules.length}`} />
              <PortalPill label="Signals" value={data.totalSignals} />
            </dl>
          }
        />

        <section className="rounded-2xl border border-border bg-card p-4" aria-label="Current lead">
          <SectionCaption>Current lead</SectionCaption>
          {primary ? <LeadModule module={primary} /> : null}
        </section>
      </div>

      <section aria-label="Portal modules" className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {data.modules.map((module) => (
          <PortalModuleCard key={module.id} module={module} />
        ))}
      </section>

      <section aria-label="Paper Radar research entries" className="space-y-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <SectionCaption>Research Module</SectionCaption>
            <h2 className="text-heading3 font-semibold text-foreground">Paper Radar</h2>
          </div>
          <Link href="/papers" className={textLinkClass}>
            Open research board
            <ArrowRight size={15} strokeWidth={1.8} aria-hidden="true" />
          </Link>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          {data.researchEntries.map((entry) => (
            <ResearchEntryCard key={entry.href} entry={entry} />
          ))}
        </div>
      </section>

      <section className="grid gap-5 border-t border-border pt-6 xl:grid-cols-[15rem_minmax(0,1fr)]">
        <nav aria-label="Boards">
          <SectionCaption className="px-2.5">Boards</SectionCaption>
          <div className="mt-0.5 space-y-0.5">
            {data.modules.map((module) => (
              <Link
                key={module.id}
                href={module.href}
                className="flex items-center justify-between gap-3 rounded-lg px-2.5 py-1.5 text-body2 text-muted-foreground outline-none transition-colors hover:bg-accent/60 hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <span className="truncate">{module.title}</span>
                <span className={`size-2 shrink-0 rounded-full ${statusDotClass[module.status]}`} aria-hidden="true" />
                <span className="sr-only">{module.status}</span>
              </Link>
            ))}
          </div>
        </nav>

        <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
          {secondary.flatMap((module) =>
            module.highlights.length
              ? module.highlights.slice(0, 2).map((highlight) => (
                  <Link
                    key={`${module.id}-${highlight.title}`}
                    href={highlight.href ?? module.href}
                    className="grid gap-1 px-4 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50 md:grid-cols-[10rem_minmax(0,1fr)_auto] md:items-center md:gap-3"
                  >
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{module.title}</span>
                    <span className="min-w-0 truncate text-body2 font-medium text-foreground">{highlight.title}</span>
                    <span className="text-label text-muted-foreground">{highlight.meta}</span>
                  </Link>
                ))
              : [
                  <div key={`${module.id}-empty`} className="grid gap-1 px-4 py-3 md:grid-cols-[10rem_minmax(0,1fr)] md:items-center md:gap-3">
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{module.title}</span>
                    <span className="text-body2 text-muted-foreground">{module.notices[0] ?? "No current items available from this source."}</span>
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
    <Link href={entry.href} aria-labelledby={titleId} aria-describedby={descriptionId} className={`group block ${cardLinkClass}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id={titleId} className="text-[15px] font-semibold text-foreground">
            {entry.title}
          </h3>
          <p id={descriptionId} className="mt-1 line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">
            {entry.description}
          </p>
        </div>
        <ArrowRight
          size={15}
          strokeWidth={1.8}
          aria-hidden="true"
          className="mt-0.5 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5"
        />
      </div>
      <div className="mt-3 rounded-xl bg-muted px-3 py-2">
        <p className="text-[11px] uppercase tracking-wider text-faint-foreground">{entry.metricLabel}</p>
        <p className="mt-0.5 text-body2 font-semibold tabular-nums text-foreground">{entry.metricValue}</p>
      </div>
      {entry.highlights.length ? <p className="mt-2 line-clamp-1 text-label text-muted-foreground">{entry.highlights[0].title}</p> : null}
    </Link>
  )
}

function LeadModule({ module }: { module: PortalModuleSummary }) {
  const Icon = moduleIcons[module.id]
  return (
    <div className="mt-2 space-y-3">
      <div className="flex items-start gap-3">
        <ModuleIconTile icon={Icon} />
        <div className="min-w-0">
          <h2 className="text-[15px] font-semibold text-foreground">{module.title}</h2>
          <p className="mt-0.5 text-[13px] leading-relaxed text-muted-foreground">{module.description}</p>
        </div>
      </div>
      <Link href={module.href} className={textLinkClass}>
        Open board
        <ArrowRight size={15} strokeWidth={1.8} aria-hidden="true" />
      </Link>
    </div>
  )
}

function PortalModuleCard({ module }: { module: PortalModuleSummary }) {
  const Icon = moduleIcons[module.id]
  return (
    <article className="flex flex-col rounded-2xl border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <ModuleIconTile icon={Icon} />
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{module.eyebrow}</p>
            <h2 className="mt-0.5 text-[15px] font-semibold text-foreground">{module.title}</h2>
          </div>
        </div>
        <StatusBadge status={module.status} />
      </div>

      <p className="mt-3 min-h-12 text-[13px] leading-relaxed text-muted-foreground">{module.description}</p>

      <dl className="mt-3 grid grid-cols-3 gap-2">
        {module.metrics.map((metric) => (
          <div key={metric.label} className="rounded-xl bg-muted px-3 py-2">
            <dt className="text-[11px] uppercase tracking-wider text-faint-foreground">{metric.label}</dt>
            <dd className="mt-0.5 truncate text-body2 font-semibold tabular-nums text-foreground">{metric.value}</dd>
          </div>
        ))}
      </dl>

      <div className="mt-3 flex-1">
        {module.highlights.length ? (
          <div className="-mx-2 space-y-0.5">
            {module.highlights.map((highlight) => (
              <Link
                key={highlight.title}
                href={highlight.href ?? module.href}
                className="block rounded-lg px-2 py-1.5 text-body2 text-foreground outline-none transition-colors hover:bg-accent/60 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <span className="line-clamp-1 font-medium">{highlight.title}</span>
                {highlight.meta ? <span className="mt-0.5 block truncate text-label text-muted-foreground">{highlight.meta}</span> : null}
              </Link>
            ))}
          </div>
        ) : (
          <p className="text-body2 text-muted-foreground">{module.notices[0] ?? "No current items available."}</p>
        )}
      </div>

      <Link href={module.href} className={`mt-4 ${textLinkClass}`}>
        View {module.title}
        <ArrowRight size={15} strokeWidth={1.8} aria-hidden="true" />
      </Link>
    </article>
  )
}

function ModuleIconTile({ icon: Icon }: { icon: typeof Newspaper }) {
  return (
    <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-accent-foreground">
      <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
    </span>
  )
}

function StatusBadge({ status }: { status: PortalModuleStatus }) {
  const Icon = statusIcon[status]
  return (
    <Badge variant={statusVariant[status]}>
      <Icon aria-hidden="true" />
      {status}
    </Badge>
  )
}

function PortalPill({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/40 px-2.5 py-1 text-label text-muted-foreground">
      <dt className="font-medium text-foreground">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  )
}
