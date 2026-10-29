"use client"

import Link from "next/link"
import { ArrowRight, ExternalLink, Newspaper } from "lucide-react"
import { useMemo, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { FilterChip } from "@/components/common/filter-chip"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { SectionCaption } from "@/components/common/section-panel"
import { Badge } from "@/components/common/badge"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import { NewsFilterPanel } from "@/features/news/components/news-filter-panel"
import { NewsList } from "@/features/news/components/news-list"
import { NewsToolbar } from "@/features/news/components/news-toolbar"
import { useNewsList } from "@/features/news/hooks/use-news-list"
import { filtersFromSearchParams, filtersToSearchParams, updateFilters } from "@/features/news/lib/news-filters"
import { formatDateTime } from "@/lib/format"
import type { NewsFilters, NewsItem, NewsViewMode } from "@/types/news"

type FacetItem = {
  name: string
  count: number
  value?: string
}

type NewsClusterPreview = {
  id: string
  title: string
  items: NewsItem[]
}

export function NewsPageClient() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false)
  const filters = useMemo(() => filtersFromSearchParams(new URLSearchParams(searchParams.toString())), [searchParams])
  const { data, isLoading, isError, error, refetch } = useNewsList(filters)

  const setFilters = (patch: Partial<NewsFilters>) => {
    const next = updateFilters(filters, patch)
    const params = filtersToSearchParams(next)
    router.replace(params.size ? `/news?${params.toString()}` : "/news", { scroll: false })
  }

  if (isLoading) {
    return <PageSkeleton />
  }

  if (isError) {
    return <ErrorState message={error instanceof Error ? error.message : "AI News failed to load."} onRetry={() => refetch()} />
  }

  if (!data) {
    return <EmptyState title="AI News is unavailable" description="No AI News payload was returned by the current data source." />
  }

  const page = data.page
  const viewMode = (filters.viewMode ?? "card") as NewsViewMode
  const totalPages = Math.max(1, Math.ceil(page.total / page.pageSize))
  const topStory = pickTopStory(data.allFiltered)
  const streamItems = topStory ? page.items.filter((item) => item.id !== topStory.id) : page.items
  const clusters = buildClusters(data.allFiltered)
  const topics = topicFacets(data.allItems)
  const companies = companyFacets(data.allItems)
  const categories = countFacet(data.allItems.map((item) => item.category))
  const sources = data.options.sourceTypes.map((sourceType) => ({
    name: sourceTypeLabel(sourceType),
    value: sourceType,
    count: data.allItems.filter((item) => item.sourceType === sourceType).length
  }))

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Agora Hub / AI News"
        title="AI News Board"
        description="Official updates, product launches, funding, policy, open-source releases, and ecosystem signals for the AI industry."
        actions={
          <dl className="flex flex-wrap gap-1.5">
            <StatPill label="Visible items" value={data.allFiltered.length} />
            <StatPill label="Sources" value={data.options.sourceTypes.length} />
            <StatPill label="Categories" value={data.options.categories.length} />
            <StatPill label="Source state" value={data.source} />
          </dl>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[15rem_minmax(0,1fr)]">
        <aside className="hidden space-y-5 xl:block" aria-label="News facets">
          <CurrentLeadCard topStory={topStory} />
          <FacetList title="Topics" items={topics} onSelect={(topic) => setFilters({ topic })} />
          <FacetList title="Source types" items={sources} onSelect={(sourceType) => setFilters({ sourceType: [sourceType as NonNullable<NewsFilters["sourceType"]>[number]] })} />
          <FacetList title="Categories" items={categories} onSelect={(category) => setFilters({ category: [category] })} />
          <FacetList title="Companies" items={companies} onSelect={(company) => setFilters({ keyword: company })} />
        </aside>

        <section className="min-w-0 space-y-4" aria-label="News stream">
          <NewsToolbar
            filters={filters}
            options={data.options}
            onChange={setFilters}
            onToggleFilters={() => setMobileFiltersOpen((value) => !value)}
          />

          <FilterBar filters={filters} onChange={setFilters} />

          {data.dataState === "fallback" ? <DegradedBanner notices={data.notices} /> : null}

          <div className={mobileFiltersOpen ? "block xl:hidden" : "hidden"}>
            <NewsFilterPanel filters={filters} options={data.options} onChange={setFilters} />
          </div>

          {topStory ? <TopStoryCard item={topStory} /> : null}
          {clusters.length ? <ClusterRail clusters={clusters} /> : null}

          <div className="flex items-center justify-between gap-3 text-label text-muted-foreground">
            <span>
              Showing {page.items.length} of {page.total} matching news items
            </span>
            <span className="tabular-nums">
              Page {page.page} of {totalPages}
            </span>
          </div>

          <NewsList items={streamItems} viewMode={viewMode} />

          <div className="flex items-center justify-between gap-3">
            <Button type="button" variant="outline" disabled={page.page <= 1} onClick={() => setFilters({ page: page.page - 1 })}>
              Previous page
            </Button>
            <Button type="button" variant="outline" disabled={!page.hasNext} onClick={() => setFilters({ page: page.page + 1 })}>
              Next page
            </Button>
          </div>
        </section>
      </div>
    </div>
  )
}

function FilterBar({ filters, onChange }: { filters: NewsFilters; onChange: (patch: Partial<NewsFilters>) => void }) {
  const periods: Array<{ label: string; value?: NewsFilters["dateRange"] }> = [
    { label: "All", value: undefined },
    { label: "Today", value: "today" },
    { label: "Week", value: "week" },
    { label: "Month", value: "month" }
  ]
  const sorts: Array<{ label: string; value: NewsFilters["sort"] }> = [
    { label: "Top", value: "heatScore" },
    { label: "Newest", value: "publishedAt" },
    { label: "Trusted", value: "qualityScore" }
  ]

  return (
    <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Period">
        {periods.map((period) => (
          <FilterChip
            key={period.label}
            active={(filters.dateRange ?? "") === (period.value ?? "")}
            onClick={() => onChange({ dateRange: period.value })}
          >
            {period.label}
          </FilterChip>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Ranking">
        {sorts.map((sort) => (
          <FilterChip
            key={sort.label}
            active={(filters.sort ?? "publishedAt") === sort.value}
            onClick={() => onChange({ sort: sort.value })}
          >
            {sort.label}
          </FilterChip>
        ))}
      </div>
    </div>
  )
}

function CurrentLeadCard({ topStory }: { topStory?: NewsItem }) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <SectionCaption>Current lead</SectionCaption>
      {topStory ? (
        <div className="mt-2 space-y-3">
          <div className="flex items-start gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-accent-foreground">
              <Newspaper size={16} strokeWidth={1.8} aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h2 className="line-clamp-3 text-body2 font-semibold text-foreground">{topStory.title}</h2>
              <p className="mt-1 text-label text-muted-foreground">{topStory.sourceName}</p>
            </div>
          </div>
          <Link
            href={`/news/${topStory.id}`}
            className="inline-flex items-center gap-1.5 rounded-sm text-body2 font-medium text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            Read brief
            <ArrowRight size={15} strokeWidth={1.8} aria-hidden="true" />
          </Link>
        </div>
      ) : (
        <p className="mt-2 text-body2 text-muted-foreground">
          No real AI News output is available from backend or local artifacts yet.
        </p>
      )}
    </section>
  )
}

function TopStoryCard({ item }: { item: NewsItem }) {
  return (
    <article className="rounded-2xl border border-border bg-card p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <SectionCaption>Top Story</SectionCaption>
          <Link href={`/news/${item.id}`} className="rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <h2 className="mt-1 max-w-4xl text-heading3 font-semibold text-foreground hover:underline">{item.title}</h2>
          </Link>
          <p className="mt-2 max-w-4xl text-body2 leading-relaxed text-muted-foreground">{item.summary}</p>
        </div>
        <Button asChild variant="outline" size="sm" className="shrink-0">
          <a href={item.sourceUrl} target="_blank" rel="noreferrer">
            Source
            <ExternalLink strokeWidth={1.8} aria-hidden="true" />
          </a>
        </Button>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-1.5">
        <Badge tone="accent">{item.category}</Badge>
        <Badge tone="neutral">{item.sourceName}</Badge>
        <Badge tone="neutral">Published {formatDateTime(item.publishedAt)}</Badge>
        <Badge tone="neutral">{item.relatedPapers?.length ?? 0} papers</Badge>
        <Badge tone="neutral">{item.relatedProjects?.length ?? 0} projects</Badge>
        <Badge tone="neutral">{item.relatedCommunityTopics?.length ?? 0} community</Badge>
      </div>
    </article>
  )
}

function ClusterRail({ clusters }: { clusters: NewsClusterPreview[] }) {
  return (
    <section className="grid gap-3 lg:grid-cols-2" aria-label="Story clusters">
      {clusters.slice(0, 2).map((cluster) => (
        <article key={cluster.id} className="rounded-2xl border border-border bg-card p-4">
          <SectionCaption>Cluster</SectionCaption>
          <h3 className="mt-1 text-[15px] font-semibold text-foreground">{cluster.title}</h3>
          <div className="mt-2 divide-y divide-border">
            {cluster.items.slice(0, 3).map((item) => (
              <Link
                key={item.id}
                href={`/news/${item.id}`}
                className="block py-2 text-[13px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <span className="font-medium text-foreground/80">{item.sourceName}:</span> {item.title}
              </Link>
            ))}
          </div>
        </article>
      ))}
    </section>
  )
}

function DegradedBanner({ notices }: { notices: string[] }) {
  return (
    <div role="status" className="flex flex-col gap-2 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Badge tone="warning">Degraded</Badge>
          <p className="text-body2 font-medium text-warning-text">No real AI News output is currently available.</p>
        </div>
        <p className="mt-1.5 text-body2 text-foreground/80">
          The board is waiting for backend or local ai_news artifacts. It will not substitute bundled mock news.
        </p>
      </div>
      {notices.length ? <p className="text-label text-muted-foreground sm:max-w-sm">{notices[notices.length - 1]}</p> : null}
    </div>
  )
}

function FacetList({ title, items, onSelect }: { title: string; items: FacetItem[]; onSelect: (value: string) => void }) {
  if (!items.length) {
    return null
  }
  return (
    <section>
      <h2 className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{title}</h2>
      <div className="mt-0.5 space-y-0.5">
        {items.slice(0, 8).map((item) => (
          <button
            key={`${title}-${item.value ?? item.name}`}
            type="button"
            onClick={() => onSelect(item.value ?? item.name)}
            className="flex w-full items-baseline justify-between gap-3 rounded-lg px-2.5 py-1.5 text-left text-body2 text-muted-foreground outline-none transition-colors hover:bg-accent/60 hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <span className="truncate">{item.name}</span>
            <span className="text-label tabular-nums text-faint-foreground">{item.count}</span>
          </button>
        ))}
      </div>
    </section>
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

function pickTopStory(items: NewsItem[]) {
  return [...items].sort((left, right) => topStoryScore(right) - topStoryScore(left))[0]
}

function topStoryScore(item: NewsItem) {
  const credibility = item.credibility === "high" ? 20 : item.credibility === "medium" ? 10 : 0
  return (item.heatScore ?? 0) * 0.5 + (item.qualityScore ?? 0) * 0.3 + credibility
}

function buildClusters(items: NewsItem[]): NewsClusterPreview[] {
  const groups = new Map<string, NewsItem[]>()
  for (const item of items) {
    const key = item.topicId ?? item.topicName
    if (!key) continue
    groups.set(key, [...(groups.get(key) ?? []), item])
  }
  return [...groups.entries()]
    .filter(([, groupItems]) => groupItems.length > 1)
    .map(([id, groupItems]) => ({
      id,
      title: groupItems[0].topicName ?? id,
      items: groupItems
    }))
}

function topicFacets(items: NewsItem[]): FacetItem[] {
  return countFacet(
    items.flatMap((item) => [item.topicName, ...item.tags]).filter((value): value is string => Boolean(value))
  )
}

function companyFacets(items: NewsItem[]): FacetItem[] {
  return countFacet(
    items
      .flatMap((item) => item.entities ?? [])
      .filter((entity) => /company|product|model/i.test(entity.type))
      .map((entity) => entity.name)
  )
}

function countFacet(values: string[]): FacetItem[] {
  const counts = new Map<string, number>()
  for (const value of values) {
    counts.set(value, (counts.get(value) ?? 0) + 1)
  }
  return [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .map(([name, count]) => ({ name, count }))
}

function sourceTypeLabel(value: string) {
  return value.replace(/[_-]/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
}
