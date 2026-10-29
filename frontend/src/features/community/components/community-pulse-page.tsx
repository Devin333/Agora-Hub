"use client"

import { ResearchMaterialContext } from "@/components/research/research-material-context"

import type { FormEvent, ReactNode } from "react"
import { useEffect, useMemo, useState } from "react"
import {
  ArrowRight,
  ExternalLink,
  Flame,
  GitBranch,
  MessageSquare,
  Radio,
  X
} from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Badge } from "@/components/common/badge"
import { FilterChip } from "@/components/common/filter-chip"
import { NativeSelect, NativeSelectOption } from "@/components/common/native-select"
import { SearchField } from "@/components/common/search-field"
import { SectionCaption } from "@/components/common/section-panel"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import {
  COMMUNITY_SIGNAL_PERIODS,
  COMMUNITY_SIGNAL_SENTIMENTS,
  COMMUNITY_SIGNAL_SORTS
} from "@/lib/community/community-signals"
import {
  communitySentimentLabel,
  communitySourceLabel
} from "@/lib/community/community-filters"
import { formatDateTime } from "@/lib/format"
import { cn } from "@/lib/utils"
import type {
  CommunitySignal,
  CommunitySignalDetailResult,
  CommunitySignalListParams,
  CommunitySignalListResult,
  DebateCluster
} from "@/types/community"

export function CommunityPulsePage({
  result,
  filters,
  sourceQuestion,
  selectedSignal,
  onChange,
  onOpenSignal,
  onCloseSignal
}: {
  result: CommunitySignalListResult
  filters: CommunitySignalListParams
  sourceQuestion?: string
  selectedSignal?: CommunitySignalDetailResult
  onChange: (patch: Partial<CommunitySignalListParams>) => void
  onOpenSignal: (signalId: string) => void
  onCloseSignal: () => void
}) {
  const [query, setQuery] = useState(filters.q ?? "")
  const topSignal = useMemo(() => [...result.allFiltered].sort((left, right) => right.heatScore - left.heatScore)[0], [result.allFiltered])

  useEffect(() => {
    setQuery(filters.q ?? "")
  }, [filters.q])

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onChange({ q: query.trim() || undefined, cursor: undefined })
  }

  return (
    <div className="space-y-5">
      <ResearchMaterialContext />
      {sourceQuestion ? (
        <div className="rounded-xl border border-border bg-muted/40 px-4 py-2.5 text-body2 text-muted-foreground">
          来自首页的问题：<span className="font-medium text-foreground">{sourceQuestion}</span>
        </div>
      ) : null}

      <PageHeader
        eyebrow="Agora Hub / Community Pulse"
        title="Community Pulse Board"
        description="Observe developer discussion, feedback, controversy, propagation paths, and real adoption signals across public communities."
        actions={
          <dl className="flex flex-wrap gap-1.5">
            <StatPill label="Signals" value={result.metrics.periodSignals} />
            <StatPill label="Sources" value={result.metrics.activeSources} />
            <StatPill label="Hot" value={result.metrics.hotSignals} />
            <StatPill label="State" value={result.source} />
          </dl>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[15rem_minmax(0,1fr)]">
        <aside className="hidden space-y-5 xl:block" aria-label="Community facets">
          <CurrentPulseCard signal={topSignal} summary={result.metrics.heatSummary} onOpenSignal={onOpenSignal} />
          <FacetList
            title="Sources"
            items={result.facets.sources.map((item) => ({ label: item.label, value: item.source, count: item.count }))}
            onSelect={(source) => onChange({ source: source as CommunitySignalListParams["source"], cursor: undefined })}
          />
          <FacetList
            title="Topics"
            items={result.facets.topics.map((item) => ({ label: item.label, value: item.topic, count: item.count }))}
            onSelect={(topic) => onChange({ topic, cursor: undefined })}
          />
          <FacetList
            title="Sentiment"
            items={result.facets.sentiments.map((item) => ({ label: item.label, value: item.sentiment, count: item.count }))}
            onSelect={(sentiment) => onChange({ sentiment: sentiment as CommunitySignalListParams["sentiment"], cursor: undefined })}
          />
        </aside>

        <section className="min-w-0 space-y-4" aria-label="Community signals stream">
          <form onSubmit={submitSearch} className="flex flex-wrap items-center gap-2">
            <SearchField
              className="min-w-[14rem] flex-1 md:max-w-[360px]"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search discussions, projects, papers, entities"
              aria-label="Search community signals"
            />

            <div className="flex flex-wrap items-center gap-2 md:ml-auto">
              <NativeSelect
                value={filters.source ?? ""}
                onChange={(event) => onChange({ source: event.target.value ? (event.target.value as CommunitySignalListParams["source"]) : undefined, cursor: undefined })}
                aria-label="Community source"
              >
                <NativeSelectOption value="">All sources</NativeSelectOption>
                {result.facets.sources.map((source) => (
                  <NativeSelectOption key={source.source} value={source.source}>
                    {source.label}
                  </NativeSelectOption>
                ))}
              </NativeSelect>

              <NativeSelect
                value={filters.topic ?? ""}
                onChange={(event) => onChange({ topic: event.target.value || undefined, cursor: undefined })}
                aria-label="Community topic"
              >
                <NativeSelectOption value="">All topics</NativeSelectOption>
                {result.facets.topics.map((topic) => (
                  <NativeSelectOption key={topic.topic} value={topic.topic}>
                    {topic.label}
                  </NativeSelectOption>
                ))}
              </NativeSelect>

              <Button type="submit" size="sm">
                Search
              </Button>
            </div>
          </form>

          <FilterBar filters={filters} onChange={onChange} />

          {result.dataState === "empty" ? <DegradedBanner notices={result.notices} /> : null}

          {topSignal ? <HotDiscussionCard signal={topSignal} onOpenSignal={onOpenSignal} /> : null}

          {result.clusters.length ? <DebateClusterPanel clusters={result.clusters} /> : null}

          <div className="flex items-center justify-between gap-3 text-label text-muted-foreground">
            <span>
              Showing {result.items.length} of {result.page.total} matching community signals
            </span>
            <span className="tabular-nums">
              Page {result.page.page}
            </span>
          </div>

          {result.items.length ? (
            <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
              {result.items.map((signal) => (
                <SignalRow key={signal.id} signal={signal} onOpenSignal={onOpenSignal} />
              ))}
            </div>
          ) : (
            <EmptyState title="No community signals" description="No public Community Pulse signals matched the current filters." />
          )}

          {result.notices.length ? (
            <div className="space-y-1 text-label text-muted-foreground">
              {result.notices.map((notice) => (
                <p key={notice}>{notice}</p>
              ))}
            </div>
          ) : null}

          <div className="flex items-center justify-between gap-3">
            <Button
              type="button"
              variant="outline"
              disabled={!filters.cursor}
              onClick={() => onChange({ cursor: undefined })}
            >
              First page
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!result.nextCursor}
              onClick={() => onChange({ cursor: result.nextCursor ?? undefined })}
            >
              Next page
            </Button>
          </div>
        </section>
      </div>

      {selectedSignal ? <SignalDetailDrawer detail={selectedSignal} onClose={onCloseSignal} /> : null}
    </div>
  )
}

function FilterBar({
  filters,
  onChange
}: {
  filters: CommunitySignalListParams
  onChange: (patch: Partial<CommunitySignalListParams>) => void
}) {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Period">
        {COMMUNITY_SIGNAL_PERIODS.map((period) => (
          <FilterChip
            key={period}
            active={(filters.period ?? "all") === period}
            onClick={() => onChange({ period, cursor: undefined })}
          >
            {period === "all" ? "All" : period[0].toUpperCase() + period.slice(1)}
          </FilterChip>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Ranking and sentiment">
        {COMMUNITY_SIGNAL_SORTS.map((sort) => (
          <FilterChip
            key={sort}
            active={(filters.sort === "trending" ? "hot" : filters.sort ?? "hot") === sort}
            onClick={() => onChange({ sort, cursor: undefined })}
          >
            {sort === "hot" ? "Hot" : sort[0].toUpperCase() + sort.slice(1)}
          </FilterChip>
        ))}
        <span className="mx-1 h-4 w-px bg-border" aria-hidden="true" />
        {COMMUNITY_SIGNAL_SENTIMENTS.map((sentiment) => (
          <FilterChip
            key={sentiment}
            active={filters.sentiment === sentiment}
            onClick={() => onChange({ sentiment: filters.sentiment === sentiment ? undefined : sentiment, cursor: undefined })}
          >
            {communitySentimentLabel(sentiment)}
          </FilterChip>
        ))}
      </div>
    </div>
  )
}

function CurrentPulseCard({
  signal,
  summary,
  onOpenSignal
}: {
  signal?: CommunitySignal
  summary: string
  onOpenSignal: (signalId: string) => void
}) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <SectionCaption>Current pulse</SectionCaption>
      {signal ? (
        <div className="mt-2 space-y-3">
          <div className="flex items-start gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-accent-foreground">
              <Radio size={16} strokeWidth={1.8} aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h2 className="line-clamp-3 text-body2 font-semibold text-foreground">{signal.title}</h2>
              <p className="mt-1 text-label text-muted-foreground">{summary}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => onOpenSignal(signal.id)}
            className="inline-flex items-center gap-1.5 rounded-sm text-body2 font-medium text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            Inspect signal
            <ArrowRight size={15} strokeWidth={1.8} aria-hidden="true" />
          </button>
        </div>
      ) : (
        <p className="mt-2 text-body2 text-muted-foreground">
          No real Community Pulse output is available from backend or local artifacts yet.
        </p>
      )}
    </section>
  )
}

function sentimentTone(sentiment: CommunitySignal["sentiment"]) {
  return sentiment === "negative" || sentiment === "controversial" ? "warning" : "neutral"
}

function HotDiscussionCard({ signal, onOpenSignal }: { signal: CommunitySignal; onOpenSignal: (signalId: string) => void }) {
  return (
    <article className="rounded-2xl border border-border bg-card p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <SectionCaption>Hot Discussion</SectionCaption>
          <button
            type="button"
            onClick={() => onOpenSignal(signal.id)}
            className="rounded-sm text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <h2 className="mt-1 max-w-4xl text-heading3 font-semibold text-foreground hover:underline">{signal.title}</h2>
          </button>
          <p className="mt-2 max-w-4xl text-body2 leading-relaxed text-muted-foreground">{signal.summary}</p>
        </div>
        <div className="grid w-full shrink-0 grid-cols-3 gap-2 sm:w-auto sm:min-w-56">
          <ScoreBox label="Heat" value={signal.heatScore} />
          <ScoreBox label="Debate" value={signal.controversyScore} />
          <ScoreBox label="Adoption" value={signal.adoptionScore} />
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-1.5">
        <Badge tone="accent">{communitySourceLabel(signal.source)}</Badge>
        <Badge tone={sentimentTone(signal.sentiment)}>{communitySentimentLabel(signal.sentiment)}</Badge>
        {signal.postedAt ? <Badge tone="neutral">Posted {formatDateTime(signal.postedAt)}</Badge> : null}
        <Badge tone="neutral">{signal.relatedProjectIds.length} projects</Badge>
        <Badge tone="neutral">{signal.relatedPaperIds.length} papers</Badge>
        <Badge tone="neutral">{signal.relatedNewsIds.length} news</Badge>
      </div>
    </article>
  )
}

function SignalRow({ signal, onOpenSignal }: { signal: CommunitySignal; onOpenSignal: (signalId: string) => void }) {
  return (
    <article className="group px-4 py-4 transition-colors hover:bg-accent/40">
      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_7.5rem]">
        <div className="min-w-0">
          <div className="flex items-start justify-between gap-3">
            <button
              type="button"
              onClick={() => onOpenSignal(signal.id)}
              className="min-w-0 rounded-sm text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              <h3 className="text-[15px] font-semibold leading-snug text-foreground group-hover:underline">{signal.title}</h3>
              <p className="mt-1 text-label text-muted-foreground">
                {signal.sourceName ?? communitySourceLabel(signal.source)}
                {signal.postedAt ? ` | ${formatDateTime(signal.postedAt)}` : null}
              </p>
            </button>
            <Badge tone={sentimentTone(signal.sentiment)}>{communitySentimentLabel(signal.sentiment)}</Badge>
          </div>

          <p className="mt-2 line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">{signal.summary}</p>

          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            <Badge tone="accent">{communitySourceLabel(signal.source)}</Badge>
            {signal.score !== undefined ? <Badge tone="neutral">{signal.score} score</Badge> : null}
            {signal.comments !== undefined ? <Badge tone="neutral">{signal.comments} comments</Badge> : null}
            {signal.topics.slice(0, 5).map((topic) => (
              <Badge key={topic} tone="neutral">
                {topic}
              </Badge>
            ))}
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <Badge tone="info">{signal.relatedProjectIds.length} projects</Badge>
            <Badge tone="info">{signal.relatedPaperIds.length} papers</Badge>
            <Badge tone="info">{signal.relatedNewsIds.length} news</Badge>
          </div>
        </div>

        <aside className="grid grid-cols-3 gap-3 border-t border-border pt-3 md:block md:space-y-3 md:border-l md:border-t-0 md:pl-4 md:pt-0" aria-label="Signal scores">
          <Metric icon={<Flame size={15} strokeWidth={1.8} />} label="Heat" value={signal.heatScore} />
          <Metric icon={<MessageSquare size={15} strokeWidth={1.8} />} label="Debate" value={signal.controversyScore} />
          <Metric icon={<GitBranch size={15} strokeWidth={1.8} />} label="Adoption" value={signal.adoptionScore} />
        </aside>
      </div>
    </article>
  )
}

function DebateClusterPanel({ clusters }: { clusters: DebateCluster[] }) {
  return (
    <section className="grid gap-3 lg:grid-cols-2" aria-label="Debate clusters">
      {clusters.slice(0, 2).map((cluster) => (
        <article key={cluster.id} className="rounded-2xl border border-border bg-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <SectionCaption>Debate Cluster</SectionCaption>
              <h3 className="mt-1 text-[15px] font-semibold text-foreground">{cluster.title}</h3>
            </div>
            <Badge tone="warning">{Math.round(cluster.controversyScore)} debate</Badge>
          </div>
          <p className="mt-2 line-clamp-2 text-[13px] leading-relaxed text-muted-foreground">{cluster.summary}</p>
          <ArgumentList label="Support" items={cluster.positiveArguments} emptyText="No public supporting arguments in the current artifact." />
          <ArgumentList label="Against" items={cluster.negativeArguments} emptyText="No public opposing arguments in the current artifact." />
          <ArgumentList label="Facts" items={cluster.neutralFacts} emptyText="No public neutral facts in the current artifact." />
        </article>
      ))}
    </section>
  )
}

function SignalDetailDrawer({ detail, onClose }: { detail: CommunitySignalDetailResult; onClose: () => void }) {
  const signal = detail.signal
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-overlay">
      <section
        role="dialog"
        aria-label="Community signal detail"
        className="rk-scroll h-full w-full max-w-2xl overflow-y-auto border-l border-border bg-background p-6"
      >
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <SectionCaption>Signal detail</SectionCaption>
            <h2 className="mt-1 text-heading2 font-semibold text-foreground">{signal.title}</h2>
            <p className="mt-1.5 text-label text-muted-foreground">
              {signal.sourceName ?? communitySourceLabel(signal.source)}
              {signal.postedAt ? ` | ${formatDateTime(signal.postedAt)}` : null}
            </p>
          </div>
          <Button type="button" variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close signal detail">
            <X size={16} strokeWidth={1.8} />
          </Button>
        </div>

        <p className="mt-4 text-body2 leading-relaxed text-muted-foreground">{signal.summary}</p>

        <div className="mt-4 grid grid-cols-3 gap-2">
          <ScoreBox label="Heat" value={signal.heatScore} />
          <ScoreBox label="Debate" value={signal.controversyScore} />
          <ScoreBox label="Adoption" value={signal.adoptionScore} />
        </div>

        <section className="mt-6 space-y-2">
          <h3 className="text-body2 font-semibold text-foreground">Evidence links</h3>
          {detail.evidenceLinks.length ? (
            <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
              {detail.evidenceLinks.map((evidence) => (
                <a
                  key={evidence.id}
                  href={evidence.url}
                  target="_blank"
                  rel="noreferrer"
                  className={cn(
                    "block px-4 py-3 text-body2 outline-none transition-colors focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50",
                    evidence.url ? "hover:bg-accent/40" : "pointer-events-none"
                  )}
                >
                  <span className="font-medium text-foreground">{evidence.title ?? evidence.sourceName ?? evidence.id}</span>
                  {evidence.excerpt ? <span className="mt-1 block text-[13px] text-muted-foreground">{evidence.excerpt}</span> : null}
                </a>
              ))}
            </div>
          ) : (
            <p className="text-body2 text-muted-foreground">No public evidence links are present in the current artifact.</p>
          )}
        </section>

        <section className="mt-6 grid gap-3 md:grid-cols-3">
          <RelationColumn title="Projects" items={detail.relatedProjects.map((item) => ({ label: item.name, href: item.url }))} />
          <RelationColumn title="Papers" items={detail.relatedPapers.map((item) => ({ label: item.title, href: item.url }))} />
          <RelationColumn title="News" items={detail.relatedNews.map((item) => ({ label: item.title, href: item.url }))} />
        </section>

        {detail.clusters.length ? (
          <section className="mt-6 space-y-2">
            <h3 className="text-body2 font-semibold text-foreground">Debate context</h3>
            <DebateClusterPanel clusters={detail.clusters} />
          </section>
        ) : null}

        {signal.url ? (
          <Button asChild variant="outline" size="sm" className="mt-6">
            <a href={signal.url} target="_blank" rel="noreferrer">
              Open source
              <ExternalLink strokeWidth={1.8} aria-hidden="true" />
            </a>
          </Button>
        ) : null}

        {detail.notices.length ? (
          <div className="mt-5 space-y-1 text-label text-muted-foreground">
            {detail.notices.map((notice) => (
              <p key={notice}>{notice}</p>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  )
}

function FacetList({
  title,
  items,
  onSelect
}: {
  title: string
  items: Array<{ label: string; value: string; count: number }>
  onSelect: (value: string) => void
}) {
  if (!items.length) return null
  return (
    <section>
      <h2 className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{title}</h2>
      <div className="mt-0.5 space-y-0.5">
        {items.slice(0, 8).map((item) => (
          <button
            key={`${title}-${item.value}`}
            type="button"
            onClick={() => onSelect(item.value)}
            className="flex w-full items-baseline justify-between gap-3 rounded-lg px-2.5 py-1.5 text-left text-body2 text-muted-foreground outline-none transition-colors hover:bg-accent/60 hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <span className="truncate">{item.label}</span>
            <span className="text-label tabular-nums text-faint-foreground">{item.count}</span>
          </button>
        ))}
      </div>
    </section>
  )
}

function ArgumentList({ label, items, emptyText }: { label: string; items: string[]; emptyText: string }) {
  return (
    <div className="mt-3 space-y-1.5">
      <SectionCaption>{label}</SectionCaption>
      {items.length ? (
        items.map((item) => (
          <p key={item} className="rounded-xl bg-muted px-3 py-2 text-[13px] text-foreground/85">
            {item}
          </p>
        ))
      ) : (
        <p className="text-label text-muted-foreground">{emptyText}</p>
      )}
    </div>
  )
}

function RelationColumn({ title, items }: { title: string; items: Array<{ label: string; href?: string }> }) {
  return (
    <div className="rounded-xl border border-border p-3">
      <SectionCaption>{title}</SectionCaption>
      <div className="mt-2 space-y-1.5">
        {items.length ? (
          items.slice(0, 4).map((item) =>
            item.href ? (
              <a
                key={item.label}
                href={item.href}
                target="_blank"
                rel="noreferrer"
                className="block rounded-sm text-[13px] text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                {item.label}
              </a>
            ) : (
              <p key={item.label} className="text-[13px] text-foreground">
                {item.label}
              </p>
            )
          )
        ) : (
          <p className="text-label text-muted-foreground">None in current artifact.</p>
        )}
      </div>
    </div>
  )
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value?: number }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center justify-center gap-1.5 text-foreground md:justify-start">
        <span className="text-muted-foreground" aria-hidden="true">{icon}</span>
        <span className="text-heading3 font-semibold tabular-nums">{value === undefined ? "n/a" : Math.round(value)}</span>
      </div>
      <p className="mt-0.5 text-center text-[11px] uppercase tracking-wider text-faint-foreground md:text-left">{label}</p>
    </div>
  )
}

function ScoreBox({ label, value }: { label: string; value?: number }) {
  return (
    <div className="rounded-xl bg-muted px-3 py-2">
      <p className="text-[11px] uppercase tracking-wider text-faint-foreground">{label}</p>
      <p className="mt-0.5 truncate text-body2 font-semibold tabular-nums text-foreground">{value === undefined ? "n/a" : Math.round(value)}</p>
    </div>
  )
}

function StatPill({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/40 px-2.5 py-1 text-label text-muted-foreground">
      <dt className="font-medium text-foreground">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  )
}

function DegradedBanner({ notices }: { notices: string[] }) {
  return (
    <div role="status" className="flex flex-col gap-2 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Badge tone="warning">Empty</Badge>
          <p className="text-body2 font-medium text-warning-text">No real Community Pulse output is currently available.</p>
        </div>
        <p className="mt-1.5 text-body2 text-foreground/80">
          The board is waiting for backend or local community_pulse artifacts. It will not substitute bundled mock signals.
        </p>
      </div>
      {notices.length ? <p className="text-label text-muted-foreground sm:max-w-sm">{notices[notices.length - 1]}</p> : null}
    </div>
  )
}
