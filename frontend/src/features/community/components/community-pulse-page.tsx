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
  Search,
  X
} from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Badge } from "@/components/common/badge"
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
    <main className="space-y-8 font-papers-research">
      <ResearchMaterialContext />
      {sourceQuestion ? (
        <div className="rounded-lg border border-[#e7dff1] bg-[#faf7ff] px-3 py-2 text-sm text-[#695d7d]">
          来自首页的问题：<span className="font-medium text-[#382758]">{sourceQuestion}</span>
        </div>
      ) : null}
      <section className="grid gap-8 py-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-end">
        <div className="min-w-0">
          <p className="mb-5 text-xs font-medium uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">
            Agora Hub / Community Pulse
          </p>
          <h1 className="max-w-5xl break-keep text-5xl font-black leading-none tracking-normal text-[#334155] sm:text-6xl lg:text-7xl dark:text-foreground">
            Community Pulse{" "}
            <span className="bg-gradient-to-r from-rose-600 via-emerald-600 to-blue-600 bg-clip-text text-transparent">
              Board
            </span>
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-7 text-[#334155]/70 dark:text-muted-foreground">
            Observe developer discussion, feedback, controversy, propagation paths, and real adoption signals across public communities.
          </p>
          <div className="mt-7 flex flex-wrap gap-2">
            <HeroPill label="Signals" value={result.metrics.periodSignals} />
            <HeroPill label="Sources" value={result.metrics.activeSources} />
            <HeroPill label="Hot" value={result.metrics.hotSignals} />
            <HeroPill label="State" value={result.source} />
          </div>
        </div>

        <CurrentPulseCard signal={topSignal} summary={result.metrics.heatSummary} onOpenSignal={onOpenSignal} />
      </section>

      <section className="grid gap-8 xl:grid-cols-[16rem_minmax(0,1fr)]">
        <aside className="hidden space-y-7 xl:block">
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

        <section className="min-w-0 space-y-5">
          <FilterBar filters={filters} onChange={onChange} />

          <form onSubmit={submitSearch} className="grid gap-3 rounded-md border border-[#dbe3dc] bg-white/85 p-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center dark:border-border dark:bg-card">
            <label className="flex min-w-0 items-center gap-2 rounded-md border border-[#dbe3dc] bg-[#f7f9f6] px-3 py-2 text-sm text-[#334155]/60 dark:border-border dark:bg-background dark:text-muted-foreground">
              <Search className="size-4 shrink-0" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search discussions, projects, papers, entities"
                className="min-w-0 flex-1 bg-transparent text-[#334155] outline-none placeholder:text-[#334155]/45 dark:text-foreground dark:placeholder:text-muted-foreground"
                aria-label="Search community signals"
              />
            </label>

            <div className="flex flex-wrap items-center gap-2">
              <select
                value={filters.source ?? ""}
                onChange={(event) => onChange({ source: event.target.value ? (event.target.value as CommunitySignalListParams["source"]) : undefined, cursor: undefined })}
                className="h-10 rounded-md border border-[#dbe3dc] bg-white px-3 text-sm text-[#334155] outline-none dark:border-border dark:bg-background dark:text-foreground"
                aria-label="Community source"
              >
                <option value="">All sources</option>
                {result.facets.sources.map((source) => (
                  <option key={source.source} value={source.source}>
                    {source.label}
                  </option>
                ))}
              </select>

              <select
                value={filters.topic ?? ""}
                onChange={(event) => onChange({ topic: event.target.value || undefined, cursor: undefined })}
                className="h-10 rounded-md border border-[#dbe3dc] bg-white px-3 text-sm text-[#334155] outline-none dark:border-border dark:bg-background dark:text-foreground"
                aria-label="Community topic"
              >
                <option value="">All topics</option>
                {result.facets.topics.map((topic) => (
                  <option key={topic.topic} value={topic.topic}>
                    {topic.label}
                  </option>
                ))}
              </select>

              <Button type="submit" size="sm">
                Search
              </Button>
            </div>
          </form>

          {result.dataState === "empty" ? <DegradedBanner notices={result.notices} /> : null}

          {topSignal ? <HotDiscussionCard signal={topSignal} onOpenSignal={onOpenSignal} /> : null}

          {result.clusters.length ? <DebateClusterPanel clusters={result.clusters} /> : null}

          <div className="flex items-center justify-between gap-3 text-xs text-[#334155]/55 dark:text-muted-foreground">
            <span>
              Showing {result.items.length} of {result.page.total} matching community signals
            </span>
            <span>
              Page {result.page.page}
            </span>
          </div>

          {result.items.length ? (
            <div className="space-y-4">
              {result.items.map((signal) => (
                <SignalRow key={signal.id} signal={signal} onOpenSignal={onOpenSignal} />
              ))}
            </div>
          ) : (
            <EmptyState title="No community signals" description="No public Community Pulse signals matched the current filters." />
          )}

          {result.notices.length ? (
            <div className="space-y-2 text-xs text-[#334155]/55 dark:text-muted-foreground">
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
      </section>

      {selectedSignal ? <SignalDetailDrawer detail={selectedSignal} onClose={onCloseSignal} /> : null}
    </main>
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
    <div className="flex flex-col gap-4 border-y border-[#d7dfd8] py-4 dark:border-border">
      <div className="flex flex-wrap items-center gap-2">
        {COMMUNITY_SIGNAL_PERIODS.map((period) => (
          <SegmentButton
            key={period}
            active={(filters.period ?? "all") === period}
            onClick={() => onChange({ period, cursor: undefined })}
          >
            {period === "all" ? "All" : period[0].toUpperCase() + period.slice(1)}
          </SegmentButton>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {COMMUNITY_SIGNAL_SORTS.map((sort) => (
          <SegmentButton
            key={sort}
            active={(filters.sort === "trending" ? "hot" : filters.sort ?? "hot") === sort}
            onClick={() => onChange({ sort, cursor: undefined })}
          >
            {sort === "hot" ? "Hot" : sort[0].toUpperCase() + sort.slice(1)}
          </SegmentButton>
        ))}
        {COMMUNITY_SIGNAL_SENTIMENTS.map((sentiment) => (
          <SegmentButton
            key={sentiment}
            active={filters.sentiment === sentiment}
            onClick={() => onChange({ sentiment: filters.sentiment === sentiment ? undefined : sentiment, cursor: undefined })}
          >
            {communitySentimentLabel(sentiment)}
          </SegmentButton>
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
    <div className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-[0_24px_60px_rgba(15,23,42,0.10)] dark:border-border dark:bg-card">
      <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">Current pulse</p>
      {signal ? (
        <div className="mt-4 space-y-4">
          <div className="flex items-start gap-3">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-[#0f172a] text-white">
              <Radio className="size-5" />
            </span>
            <div className="min-w-0">
              <h2 className="line-clamp-2 text-lg font-semibold text-[#334155] dark:text-foreground">{signal.title}</h2>
              <p className="mt-1 text-sm text-[#334155]/60 dark:text-muted-foreground">{summary}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => onOpenSignal(signal.id)}
            className="inline-flex items-center gap-2 text-sm font-semibold text-emerald-700 hover:text-emerald-800"
          >
            Inspect signal
            <ArrowRight className="size-4" />
          </button>
        </div>
      ) : (
        <p className="mt-4 text-sm leading-6 text-[#334155]/60 dark:text-muted-foreground">
          No real Community Pulse output is available from backend or local artifacts yet.
        </p>
      )}
    </div>
  )
}

function HotDiscussionCard({ signal, onOpenSignal }: { signal: CommunitySignal; onOpenSignal: (signalId: string) => void }) {
  return (
    <article className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-sm dark:border-border dark:bg-card">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Hot Discussion</p>
          <button type="button" onClick={() => onOpenSignal(signal.id)} className="text-left">
            <h2 className="mt-2 max-w-4xl text-2xl font-semibold tracking-normal text-[#334155] hover:text-emerald-700 dark:text-foreground">
              {signal.title}
            </h2>
          </button>
          <p className="mt-3 max-w-4xl text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">{signal.summary}</p>
        </div>
        <div className="grid w-full shrink-0 grid-cols-3 gap-2 sm:w-auto sm:min-w-56">
          <ScoreBox label="Heat" value={signal.heatScore} />
          <ScoreBox label="Debate" value={signal.controversyScore} />
          <ScoreBox label="Adoption" value={signal.adoptionScore} />
        </div>
      </div>
      <div className="mt-5 flex flex-wrap items-center gap-2">
        <Badge tone="accent">{communitySourceLabel(signal.source)}</Badge>
        <Badge tone={signal.sentiment === "negative" || signal.sentiment === "controversial" ? "warning" : "neutral"}>
          {communitySentimentLabel(signal.sentiment)}
        </Badge>
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
    <article className="rounded-md border border-[#dbe3dc] bg-white/85 p-5 transition-colors hover:bg-white dark:border-border dark:bg-card">
      <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_9rem]">
        <div className="min-w-0">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <button type="button" onClick={() => onOpenSignal(signal.id)} className="min-w-0 text-left">
              <h3 className="text-xl font-semibold leading-tight text-[#334155] hover:text-emerald-700 dark:text-foreground">{signal.title}</h3>
              <p className="mt-1 text-xs text-[#334155]/55 dark:text-muted-foreground">
                {signal.sourceName ?? communitySourceLabel(signal.source)}
                {signal.postedAt ? ` | ${formatDateTime(signal.postedAt)}` : null}
              </p>
            </button>
            <Badge tone={signal.sentiment === "negative" || signal.sentiment === "controversial" ? "warning" : "neutral"}>
              {communitySentimentLabel(signal.sentiment)}
            </Badge>
          </div>

          <p className="mt-3 line-clamp-3 text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">{signal.summary}</p>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Badge tone="accent">{communitySourceLabel(signal.source)}</Badge>
            {signal.score !== undefined ? <Badge tone="neutral">{signal.score} score</Badge> : null}
            {signal.comments !== undefined ? <Badge tone="neutral">{signal.comments} comments</Badge> : null}
            {signal.topics.slice(0, 5).map((topic) => (
              <Badge key={topic} tone="neutral">
                {topic}
              </Badge>
            ))}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Badge tone="info">{signal.relatedProjectIds.length} projects</Badge>
            <Badge tone="info">{signal.relatedPaperIds.length} papers</Badge>
            <Badge tone="info">{signal.relatedNewsIds.length} news</Badge>
          </div>
        </div>

        <aside className="grid grid-cols-3 gap-3 border-t border-[#dbe3dc] pt-4 md:block md:space-y-4 md:border-l md:border-t-0 md:pl-5 md:pt-0 dark:border-border">
          <Metric icon={<Flame className="size-4" />} label="Heat" value={signal.heatScore} />
          <Metric icon={<MessageSquare className="size-4" />} label="Debate" value={signal.controversyScore} />
          <Metric icon={<GitBranch className="size-4" />} label="Adoption" value={signal.adoptionScore} />
        </aside>
      </div>
    </article>
  )
}

function DebateClusterPanel({ clusters }: { clusters: DebateCluster[] }) {
  return (
    <section className="grid gap-3 lg:grid-cols-2">
      {clusters.slice(0, 2).map((cluster) => (
        <article key={cluster.id} className="rounded-md border border-[#dbe3dc] bg-white/75 p-4 dark:border-border dark:bg-card">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Debate Cluster</p>
              <h3 className="mt-2 text-base font-semibold text-[#334155] dark:text-foreground">{cluster.title}</h3>
            </div>
            <Badge tone="warning">{Math.round(cluster.controversyScore)} debate</Badge>
          </div>
          <p className="mt-3 line-clamp-2 text-sm leading-6 text-[#334155]/65 dark:text-muted-foreground">{cluster.summary}</p>
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
    <div className="fixed inset-0 z-50 flex justify-end bg-[#0f172a]/30">
      <section
        role="dialog"
        aria-label="Community signal detail"
        className="h-full w-full max-w-2xl overflow-y-auto border-l border-[#dbe3dc] bg-white p-6 shadow-2xl dark:border-border dark:bg-background"
      >
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Signal detail</p>
            <h2 className="mt-2 text-2xl font-semibold text-[#334155] dark:text-foreground">{signal.title}</h2>
            <p className="mt-2 text-sm text-[#334155]/60 dark:text-muted-foreground">
              {signal.sourceName ?? communitySourceLabel(signal.source)}
              {signal.postedAt ? ` | ${formatDateTime(signal.postedAt)}` : null}
            </p>
          </div>
          <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label="Close signal detail">
            <X className="size-5" />
          </Button>
        </div>

        <p className="mt-5 text-sm leading-7 text-[#334155]/72 dark:text-muted-foreground">{signal.summary}</p>

        <div className="mt-5 grid grid-cols-3 gap-2">
          <ScoreBox label="Heat" value={signal.heatScore} />
          <ScoreBox label="Debate" value={signal.controversyScore} />
          <ScoreBox label="Adoption" value={signal.adoptionScore} />
        </div>

        <section className="mt-7 space-y-3">
          <h3 className="text-sm font-semibold text-[#334155] dark:text-foreground">Evidence links</h3>
          {detail.evidenceLinks.length ? (
            detail.evidenceLinks.map((evidence) => (
              <a
                key={evidence.id}
                href={evidence.url}
                target="_blank"
                rel="noreferrer"
                className={cn(
                  "block rounded-md border border-[#dbe3dc] p-3 text-sm dark:border-border",
                  evidence.url ? "hover:bg-[#f7f9f6] dark:hover:bg-card" : "pointer-events-none"
                )}
              >
                <span className="font-medium text-[#334155] dark:text-foreground">{evidence.title ?? evidence.sourceName ?? evidence.id}</span>
                {evidence.excerpt ? <span className="mt-1 block text-[#334155]/65 dark:text-muted-foreground">{evidence.excerpt}</span> : null}
              </a>
            ))
          ) : (
            <p className="rounded-md border border-dashed border-[#dbe3dc] p-3 text-sm text-[#334155]/60 dark:border-border dark:text-muted-foreground">
              No public evidence links are present in the current artifact.
            </p>
          )}
        </section>

        <section className="mt-7 grid gap-3 md:grid-cols-3">
          <RelationColumn title="Projects" items={detail.relatedProjects.map((item) => ({ label: item.name, href: item.url }))} />
          <RelationColumn title="Papers" items={detail.relatedPapers.map((item) => ({ label: item.title, href: item.url }))} />
          <RelationColumn title="News" items={detail.relatedNews.map((item) => ({ label: item.title, href: item.url }))} />
        </section>

        {detail.clusters.length ? (
          <section className="mt-7 space-y-3">
            <h3 className="text-sm font-semibold text-[#334155] dark:text-foreground">Debate context</h3>
            <DebateClusterPanel clusters={detail.clusters} />
          </section>
        ) : null}

        {signal.url ? (
          <a
            href={signal.url}
            target="_blank"
            rel="noreferrer"
            className="mt-7 inline-flex items-center gap-2 rounded-md border border-[#dbe3dc] px-3 py-2 text-sm font-semibold text-[#334155] hover:bg-[#f7f9f6] dark:border-border dark:text-foreground dark:hover:bg-card"
          >
            Open source
            <ExternalLink className="size-4" />
          </a>
        ) : null}

        {detail.notices.length ? (
          <div className="mt-6 space-y-2 text-xs text-[#334155]/55 dark:text-muted-foreground">
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
    <section className="space-y-3">
      <h2 className="font-mono text-[11px] font-semibold uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">{title}</h2>
      <div className="space-y-2">
        {items.slice(0, 8).map((item) => (
          <button
            key={`${title}-${item.value}`}
            type="button"
            onClick={() => onSelect(item.value)}
            className="flex w-full items-baseline justify-between gap-3 text-left text-sm text-[#334155]/68 hover:text-[#334155] dark:text-muted-foreground dark:hover:text-foreground"
          >
            <span className="truncate">{item.label}</span>
            <span className="font-mono text-[11px]">{item.count}</span>
          </button>
        ))}
      </div>
    </section>
  )
}

function ArgumentList({ label, items, emptyText }: { label: string; items: string[]; emptyText: string }) {
  return (
    <div className="mt-4 space-y-2">
      <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">{label}</p>
      {items.length ? (
        items.map((item) => (
          <p key={item} className="rounded-md bg-[#f7f9f6] px-3 py-2 text-sm text-[#334155]/70 dark:bg-background dark:text-muted-foreground">
            {item}
          </p>
        ))
      ) : (
        <p className="text-xs text-[#334155]/50 dark:text-muted-foreground">{emptyText}</p>
      )}
    </div>
  )
}

function RelationColumn({ title, items }: { title: string; items: Array<{ label: string; href?: string }> }) {
  return (
    <div className="rounded-md border border-[#dbe3dc] p-3 dark:border-border">
      <h3 className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">{title}</h3>
      <div className="mt-3 space-y-2">
        {items.length ? (
          items.slice(0, 4).map((item) =>
            item.href ? (
              <a key={item.label} href={item.href} target="_blank" rel="noreferrer" className="block text-sm text-[#334155] hover:text-emerald-700 dark:text-foreground">
                {item.label}
              </a>
            ) : (
              <p key={item.label} className="text-sm text-[#334155] dark:text-foreground">
                {item.label}
              </p>
            )
          )
        ) : (
          <p className="text-xs text-[#334155]/55 dark:text-muted-foreground">None in current artifact.</p>
        )}
      </div>
    </div>
  )
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value?: number }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center justify-center gap-2 text-[#334155] md:justify-start dark:text-foreground">
        <span className="text-[#334155]/55 dark:text-muted-foreground">{icon}</span>
        <span className="font-mono text-lg font-semibold">{value === undefined ? "n/a" : Math.round(value)}</span>
      </div>
      <p className="mt-1 text-center font-mono text-[10px] uppercase tracking-normal text-[#334155]/55 md:text-left dark:text-muted-foreground">{label}</p>
    </div>
  )
}

function ScoreBox({ label, value }: { label: string; value?: number }) {
  return (
    <div className="rounded-md border border-[#edf1ed] bg-[#f7f9f6] px-3 py-2 dark:border-border dark:bg-background">
      <p className="text-[11px] uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">{label}</p>
      <p className="mt-1 truncate text-sm font-semibold text-[#334155] dark:text-foreground">{value === undefined ? "n/a" : Math.round(value)}</p>
    </div>
  )
}

function SegmentButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "h-8 rounded-full border px-3 text-sm font-semibold transition-colors",
        active
          ? "border-[#315d8a] bg-[#315d8a] text-white"
          : "border-[#d7dfd8] bg-white text-[#334155]/65 hover:border-[#315d8a]/40 hover:text-[#334155] dark:border-border dark:bg-card dark:text-muted-foreground"
      )}
    >
      {children}
    </button>
  )
}

function HeroPill({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-[#dbe5dd] bg-white/90 px-3 py-1 text-xs text-[#334155]/70 shadow-sm dark:border-border dark:bg-card dark:text-muted-foreground">
      <span className="font-medium text-[#334155] dark:text-foreground">{label}</span>
      {value}
    </span>
  )
}

function DegradedBanner({ notices }: { notices: string[] }) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-amber-200 bg-amber-50 p-4 text-amber-950 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Badge tone="warning">Empty</Badge>
          <p className="text-sm font-medium">No real Community Pulse output is currently available.</p>
        </div>
        <p className="mt-2 text-sm leading-6">
          The board is waiting for backend or local community_pulse artifacts. It will not substitute bundled mock signals.
        </p>
      </div>
      {notices.length ? <p className="text-xs sm:max-w-sm">{notices[notices.length - 1]}</p> : null}
    </div>
  )
}
