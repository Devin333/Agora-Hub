"use client"

import Link from "next/link"
import { ArrowRight, ExternalLink, Newspaper } from "lucide-react"
import { useMemo, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { Badge } from "@/components/common/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { NewsFilterPanel } from "@/features/news/components/news-filter-panel"
import { NewsList } from "@/features/news/components/news-list"
import { NewsToolbar } from "@/features/news/components/news-toolbar"
import { useNewsList } from "@/features/news/hooks/use-news-list"
import { filtersFromSearchParams, filtersToSearchParams, updateFilters } from "@/features/news/lib/news-filters"
import { formatDateTime } from "@/lib/format"
import { cn } from "@/lib/utils"
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
    <main className="space-y-8">
      <section className="grid gap-8 py-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-end">
        <div className="min-w-0">
          <p className="mb-5 text-xs font-medium uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">
            Agora Hub / AI News
          </p>
          <h1 className="max-w-5xl break-keep text-5xl font-black leading-none tracking-normal text-[#334155] sm:text-6xl lg:text-7xl dark:text-foreground">
            AI News{" "}
            <span className="bg-gradient-to-r from-emerald-600 via-teal-600 to-blue-600 bg-clip-text text-transparent">
              Board
            </span>
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-7 text-[#334155]/70 dark:text-muted-foreground">
            Official updates, product launches, funding, policy, open-source releases, and ecosystem signals for the AI industry.
          </p>
          <div className="mt-7 flex flex-wrap gap-2">
            <HeroPill label="Visible items" value={data.allFiltered.length} />
            <HeroPill label="Sources" value={data.options.sourceTypes.length} />
            <HeroPill label="Categories" value={data.options.categories.length} />
            <HeroPill label="Source state" value={data.source} />
          </div>
        </div>

        <CurrentLeadCard topStory={topStory} />
      </section>

      <section className="grid gap-8 xl:grid-cols-[16rem_minmax(0,1fr)]">
        <aside className="hidden space-y-7 xl:block">
          <FacetList title="Topics" items={topics} onSelect={(topic) => setFilters({ topic })} />
          <FacetList title="Source types" items={sources} onSelect={(sourceType) => setFilters({ sourceType: [sourceType as NonNullable<NewsFilters["sourceType"]>[number]] })} />
          <FacetList title="Categories" items={categories} onSelect={(category) => setFilters({ category: [category] })} />
          <FacetList title="Companies" items={companies} onSelect={(company) => setFilters({ keyword: company })} />
        </aside>

        <section className="min-w-0 space-y-5">
          <FilterBar filters={filters} onChange={setFilters} />

          <NewsToolbar
            filters={filters}
            options={data.options}
            onChange={setFilters}
            onToggleFilters={() => setMobileFiltersOpen((value) => !value)}
          />

          {data.dataState === "fallback" ? <DegradedBanner notices={data.notices} /> : null}

          <div className={mobileFiltersOpen ? "block xl:hidden" : "hidden"}>
            <NewsFilterPanel filters={filters} options={data.options} onChange={setFilters} />
          </div>

          {topStory ? <TopStoryCard item={topStory} /> : null}
          {clusters.length ? <ClusterRail clusters={clusters} /> : null}

          <div className="flex items-center justify-between gap-3 text-xs text-[#334155]/55 dark:text-muted-foreground">
            <span>
              Showing {page.items.length} of {page.total} matching news items
            </span>
            <span>
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
      </section>
    </main>
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
    <div className="flex flex-col gap-4 border-y border-[#d7dfd8] py-4 md:flex-row md:items-center md:justify-between dark:border-border">
      <div className="flex flex-wrap items-center gap-2">
        {periods.map((period) => (
          <button
            key={period.label}
            type="button"
            onClick={() => onChange({ dateRange: period.value })}
            className={cn(
              "h-8 rounded-full border px-3 text-sm font-semibold transition-colors",
              (filters.dateRange ?? "") === (period.value ?? "")
                ? "border-[#315d8a] bg-[#315d8a] text-white"
                : "border-[#d7dfd8] bg-white text-[#334155]/65 hover:border-[#315d8a]/40 hover:text-[#334155] dark:border-border dark:bg-card dark:text-muted-foreground"
            )}
          >
            {period.label}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {sorts.map((sort) => (
          <button
            key={sort.label}
            type="button"
            onClick={() => onChange({ sort: sort.value })}
            className={cn(
              "h-8 rounded-full border px-3 text-sm font-semibold transition-colors",
              (filters.sort ?? "publishedAt") === sort.value
                ? "border-emerald-700 bg-emerald-700 text-white"
                : "border-[#d7dfd8] bg-white text-[#334155]/65 hover:border-emerald-700/40 hover:text-[#334155] dark:border-border dark:bg-card dark:text-muted-foreground"
            )}
          >
            {sort.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function CurrentLeadCard({ topStory }: { topStory?: NewsItem }) {
  return (
    <div className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-[0_24px_60px_rgba(15,23,42,0.10)] dark:border-border dark:bg-card">
      <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">Current lead</p>
      {topStory ? (
        <div className="mt-4 space-y-4">
          <div className="flex items-start gap-3">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-[#0f172a] text-white">
              <Newspaper className="size-5" />
            </span>
            <div className="min-w-0">
              <h2 className="line-clamp-2 text-lg font-semibold text-[#334155] dark:text-foreground">{topStory.title}</h2>
              <p className="mt-1 text-sm text-[#334155]/60 dark:text-muted-foreground">{topStory.sourceName}</p>
            </div>
          </div>
          <Link href={`/news/${topStory.id}`} className="inline-flex items-center gap-2 text-sm font-semibold text-emerald-700 hover:text-emerald-800">
            Read brief
            <ArrowRight className="size-4" />
          </Link>
        </div>
      ) : (
        <p className="mt-4 text-sm leading-6 text-[#334155]/60 dark:text-muted-foreground">
          No real AI News output is available from backend or local artifacts yet.
        </p>
      )}
    </div>
  )
}

function TopStoryCard({ item }: { item: NewsItem }) {
  return (
    <article className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-sm dark:border-border dark:bg-card">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Top Story</p>
          <Link href={`/news/${item.id}`}>
            <h2 className="mt-2 max-w-4xl text-2xl font-semibold tracking-normal text-[#334155] hover:text-emerald-700 dark:text-foreground">
              {item.title}
            </h2>
          </Link>
          <p className="mt-3 max-w-4xl text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">{item.summary}</p>
        </div>
        <a
          href={item.sourceUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex shrink-0 items-center gap-2 rounded-md border border-[#dbe3dc] bg-white px-3 py-2 text-sm text-[#334155] hover:bg-[#f7f9f6] dark:border-border dark:bg-background dark:text-foreground"
        >
          Source
          <ExternalLink className="size-4" />
        </a>
      </div>
      <div className="mt-5 flex flex-wrap items-center gap-2">
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
    <section className="grid gap-3 lg:grid-cols-2">
      {clusters.slice(0, 2).map((cluster) => (
        <article key={cluster.id} className="rounded-md border border-[#dbe3dc] bg-white/75 p-4 dark:border-border dark:bg-card">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Cluster</p>
          <h3 className="mt-2 text-base font-semibold text-[#334155] dark:text-foreground">{cluster.title}</h3>
          <div className="mt-3 space-y-2">
            {cluster.items.slice(0, 3).map((item) => (
              <Link key={item.id} href={`/news/${item.id}`} className="block text-sm text-[#334155]/70 hover:text-emerald-700 dark:text-muted-foreground">
                {item.sourceName}: {item.title}
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
    <Card className="flex flex-col gap-2 border-amber-200 bg-amber-50 p-4 text-amber-950 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Badge tone="warning">Degraded</Badge>
          <p className="text-sm font-medium">No real AI News output is currently available.</p>
        </div>
        <p className="mt-2 text-sm leading-6">
          The board is waiting for backend or local ai_news artifacts. It will not substitute bundled mock news.
        </p>
      </div>
      {notices.length ? <p className="text-xs sm:max-w-sm">{notices[notices.length - 1]}</p> : null}
    </Card>
  )
}

function FacetList({ title, items, onSelect }: { title: string; items: FacetItem[]; onSelect: (value: string) => void }) {
  if (!items.length) {
    return null
  }
  return (
    <section className="space-y-3">
      <h2 className="font-mono text-[11px] font-semibold uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">{title}</h2>
      <div className="space-y-2">
        {items.slice(0, 8).map((item) => (
          <button
            key={`${title}-${item.value ?? item.name}`}
            type="button"
            onClick={() => onSelect(item.value ?? item.name)}
            className="flex w-full items-baseline justify-between gap-3 text-left text-sm text-[#334155]/68 hover:text-[#334155] dark:text-muted-foreground dark:hover:text-foreground"
          >
            <span className="truncate">{item.name}</span>
            <span className="font-mono text-[11px]">{item.count}</span>
          </button>
        ))}
      </div>
    </section>
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
