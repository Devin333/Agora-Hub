import Link from "next/link"
import { ArrowRight, CircleAlert, CircleCheck, CircleDashed, Network } from "lucide-react"
import { SectionPanel } from "@/components/common/section-panel"
import { PageHeader } from "@/components/layout/page-header"
import { Badge, type BadgeVariant } from "@/components/ui/badge"
import type { EvidenceGraphData, EvidenceGraphSection, PortalModuleStatus } from "@/features/portal/portal-home-data"

const listFrameClass = "divide-y divide-border overflow-hidden rounded-xl border border-border"
const rowLinkClass =
  "block px-4 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50"

export function EvidenceGraphPage({ data }: { data: EvidenceGraphData }) {
  const total = data.sections.reduce((count, section) => count + section.count, 0)

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Trends / Evidence"
        title="Cross-board Evidence Graph"
        description={data.summary}
        actions={
          <div className="flex items-center gap-2.5 rounded-xl border border-border px-3 py-1.5">
            <Network size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />
            <span className="text-label text-muted-foreground">Graph scope</span>
            <span className="text-body1 font-semibold tabular-nums text-foreground">{total}</span>
          </div>
        }
      />
      <p className="text-label text-muted-foreground">
        Paper, project, news, and community evidence are shown as structured sections in this MVP view.
      </p>

      {data.notices.length ? (
        <section className="rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-body2 text-foreground">
          {data.notices.slice(0, 3).join(" ")}
        </section>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {data.sections.map((section) => (
          <EvidenceSectionCard key={section.title} section={section} />
        ))}
      </section>

      <section className="grid gap-6 border-t border-border pt-6 xl:grid-cols-[16rem_minmax(0,1fr)]">
        <SectionPanel title="Signal mix" framed={false}>
          <nav aria-label="Signal mix" className={listFrameClass}>
            {data.sections.map((section) => (
              <Link key={section.title} href={section.href} className={`${rowLinkClass} flex items-center justify-between gap-3 text-body2`}>
                <span className="truncate text-foreground">{section.title}</span>
                <span className="text-label tabular-nums text-muted-foreground">{section.count}</span>
              </Link>
            ))}
          </nav>
        </SectionPanel>

        <SectionPanel title="Timeline" framed={false}>
          {data.timeline.length ? (
            <ol className={listFrameClass}>
              {data.timeline.map((item) => (
                <li key={item.title}>
                  <Link
                    href={item.href ?? "/topics?view=evidence-graph"}
                    className={`${rowLinkClass} grid gap-1 md:grid-cols-[minmax(0,1fr)_8rem] md:items-center md:gap-3`}
                  >
                    <span className="min-w-0 truncate text-body2 font-medium text-foreground">{item.title}</span>
                    <span className="text-label text-muted-foreground md:text-right">{item.meta}</span>
                  </Link>
                </li>
              ))}
            </ol>
          ) : (
            <p className="rounded-xl border border-dashed border-border px-4 py-6 text-center text-body2 text-muted-foreground">
              No evidence timeline is available from the current data sources.
            </p>
          )}
        </SectionPanel>
      </section>
    </div>
  )
}

function EvidenceSectionCard({ section }: { section: EvidenceGraphSection }) {
  return (
    <article className="flex min-w-0 flex-col rounded-2xl border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-label text-muted-foreground">Evidence</p>
          <h2 className="mt-0.5 text-[15px] font-semibold text-foreground">{section.title}</h2>
        </div>
        <StatusBadge status={section.status} />
      </div>
      <p className="mt-3 text-heading1 font-semibold tabular-nums text-foreground">{section.count}</p>
      <div className="mt-3 flex-1">
        {section.items.length ? (
          <div className={listFrameClass}>
            {section.items.slice(0, 3).map((item) => (
              <Link key={item.title} href={item.href ?? section.href} className={rowLinkClass}>
                <span className="line-clamp-1 text-body2 font-medium text-foreground">{item.title}</span>
                {item.meta ? <span className="mt-0.5 block truncate text-label text-muted-foreground">{item.meta}</span> : null}
              </Link>
            ))}
          </div>
        ) : (
          <p className="rounded-xl border border-dashed border-border px-4 py-3 text-body2 text-muted-foreground">
            No {section.title.toLowerCase()} evidence is available.
          </p>
        )}
      </div>
      <Link
        href={section.href}
        className="mt-4 inline-flex w-fit items-center gap-1.5 rounded-md text-body2 font-medium text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        Open source view
        <ArrowRight size={15} strokeWidth={1.8} aria-hidden="true" />
      </Link>
    </article>
  )
}

const statusAppearance: Record<PortalModuleStatus, { variant: BadgeVariant; icon: typeof CircleCheck }> = {
  ready: { variant: "success", icon: CircleCheck },
  empty: { variant: "muted", icon: CircleDashed },
  degraded: { variant: "warning", icon: CircleAlert },
}

function StatusBadge({ status }: { status: PortalModuleStatus }) {
  const { variant, icon: Icon } = statusAppearance[status] ?? statusAppearance.degraded
  return (
    <Badge variant={variant}>
      <Icon strokeWidth={1.8} aria-hidden="true" />
      {status}
    </Badge>
  )
}
