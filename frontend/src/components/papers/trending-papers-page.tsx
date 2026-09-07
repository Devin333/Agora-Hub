"use client"

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { BarChart3, ChevronLeft, ChevronRight, FileText, Github, Quote } from "lucide-react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { PapersDomainSidebar } from "@/components/papers/papers-domain-sidebar"
import { PapersHero } from "@/components/papers/papers-hero"
import { PapersMicrobar } from "@/components/papers/papers-microbar"
import { PaperDetailDrawer } from "@/components/papers/shared/paper-detail-drawer"
import { PaperPeriodTabs } from "@/components/papers/shared/paper-period-tabs"
import { PaperStream } from "@/components/papers/shared/paper-stream"
import { Button } from "@/components/ui/button"
import { translate } from "@/lib/i18n"
import type { TranslationKey } from "@/lib/i18n/translations"
import { fetchPapers } from "@/lib/papers/api"
import { localizedResearchNotice, papersCopy, t } from "@/lib/papers/copy"
import { paperFeatureFilters, parsePaperFeatureFilters, serializePaperFeatureFilters, type PaperFeatureFilter } from "@/lib/papers/filters"
import { queryPapers, type PaperQuery } from "@/lib/papers/query"
import { hasSavedPaperScroll, restorePaperScroll } from "@/lib/papers/discovery-navigation"
import { buildPaperPortalMetrics, deriveMethodAreaDomains, deriveTopPaperDomains, type PaperPortalMetrics } from "@/lib/papers/metrics"
import type { Locale, Paper, PaperListResult, PaperPeriod, PaperSort } from "@/lib/papers/types"

const PAPER_DASHBOARD_LIMIT = 5000
const PAPER_PAGE_SIZE = 15
type PaperDataContext = { source?: string; collectedAt?: string }

export type PapersDiscoveryViewModel = {
  locale: Locale
  papers: Paper[]
  metrics: PaperPortalMetrics
  query: string
  topic: string
  from: string
  to: string
  question: string
  view: "discover" | "reading" | "compare"
  returnTo: string
  catalog: Paper[]
  source?: string
  collectedAt?: string
  latestPublishedAt?: string
  hasDataIssue: boolean
  period: PaperPeriod
  sort: PaperSort
  filters: PaperFeatureFilter[]
  isLoading: boolean
  emptyDescription: string
  notice: ReactNode
  pagination: ReactNode
  drawer: ReactNode
  onSearch: (query: string) => void
  onTopicChange: (topic: string) => void
  onDateRangeChange: (from: string, to: string) => void
  onQuestionClear: () => void
  onViewChange: (view: "discover" | "reading" | "compare") => void
  onPeriodChange: (period: PaperPeriod) => void
  onSortChange: (sort: PaperSort) => void
  onFilterToggle: (filter: PaperFeatureFilter) => void
  onReset: () => void
  onPreview: (paper: Paper) => void
}

export function TrendingPapersPage({ locale, papers, renderView }: {
  locale: Locale
  papers: Paper[]
  renderView?: (model: PapersDiscoveryViewModel) => ReactNode
}) {
  const router = useRouter()
  const routerReplace = router.replace
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const searchText = searchParams.toString()
  const period = parsePeriod(searchParams.get("period"))
  const sort = parseSort(searchParams.get("sort"), Boolean(renderView && searchParams.get("q")))
  const page = parsePage(searchParams.get("page"))
  const query = searchParams.get("q") ?? ""
  const topic = searchParams.get("topic") ?? ""
  const from = searchParams.get("from") ?? ""
  const to = searchParams.get("to") ?? ""
  const question = searchParams.get("question") ?? ""
  const requestedView = searchParams.get("view")
  const view = requestedView === "reading" || requestedView === "compare" ? requestedView : "discover"
  const featureFilters = useMemo(() => parsePaperFeatureFilters(new URLSearchParams(searchText).get("has")), [searchText])
  const deepLinkedPaperId = searchParams.get("paper")
  const initialPublishedPapers = useMemo(() => fallbackPaperQuery(papers, { query, period, sort, topic, from, to, has: featureFilters }), [featureFilters, papers, period, query, sort, topic, from, to])
  const [dashboardPapers, setDashboardPapers] = useState(initialPublishedPapers)
  const [visiblePapers, setVisiblePapers] = useState(initialPublishedPapers.slice((page - 1) * PAPER_PAGE_SIZE, page * PAPER_PAGE_SIZE))
  const [paperTotalCount, setPaperTotalCount] = useState(initialPublishedPapers.length)
  const [selectedPaperId, setSelectedPaperId] = useState<string | null>(deepLinkedPaperId)
  const [isLoading, setIsLoading] = useState(false)
  const [hasDataIssue, setHasDataIssue] = useState(false)
  const [notices, setNotices] = useState<string[]>([])
  const [dataContext, setDataContext] = useState<PaperDataContext>({})
  const [latestPublishedAt, setLatestPublishedAt] = useState(() => latestPaperTimestamp(papers))
  const [loaded, setLoaded] = useState(false)
  const pageOffset = (page - 1) * PAPER_PAGE_SIZE
  const portalMetrics = useMemo(
    () => buildPaperPortalMetrics(dashboardPapers, paperTotalCount),
    [dashboardPapers, paperTotalCount]
  )
  const topDomains = useMemo(() => deriveTopPaperDomains(dashboardPapers), [dashboardPapers])
  const methodAreas = useMemo(() => deriveMethodAreaDomains(dashboardPapers), [dashboardPapers])
  const selectedPaper = useMemo(
    () =>
      visiblePapers.find((paper) => paper.id === selectedPaperId || paper.slug === selectedPaperId) ??
      dashboardPapers.find((paper) => paper.id === selectedPaperId || paper.slug === selectedPaperId) ??
      null,
    [dashboardPapers, selectedPaperId, visiblePapers]
  )
  const updateQuery = useCallback((updates: Record<string, string | null>) => {
    const nextParams = new URLSearchParams(searchText)
    for (const [key, value] of Object.entries(updates)) {
      if (value === null || value === "") {
        nextParams.delete(key)
      } else {
        nextParams.set(key, value)
      }
    }
    const nextQuery = nextParams.toString()
    routerReplace(nextQuery ? `${pathname}?${nextQuery}` : pathname, { scroll: false })
  }, [pathname, routerReplace, searchText])
  const updatePage = useCallback((nextPage: number) => {
    updateQuery({ page: nextPage <= 1 ? null : String(nextPage), paper: null })
    if (renderView && window.scrollY > 0) window.scrollTo({ top: 0, behavior: "instant" })
  }, [updateQuery, renderView])

  useEffect(() => {
    setSelectedPaperId(deepLinkedPaperId)
  }, [deepLinkedPaperId])

  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setHasDataIssue(false)
    const has = serializePaperFeatureFilters(featureFilters)
    const refinements = { ...(topic ? { topic } : {}), ...(from ? { from } : {}), ...(to ? { to } : {}) }
    Promise.allSettled([
      fetchPapers({ q: query, period, sort, ...refinements, ...(has ? { has } : {}), limit: PAPER_PAGE_SIZE, offset: pageOffset }),
      fetchPapers({ q: query, period, sort, ...refinements, ...(has ? { has } : {}), limit: PAPER_DASHBOARD_LIMIT })
    ])
      .then(([pageSettled, dashboardSettled]) => {
        if (cancelled) {
          return
        }

        const pageResult = fulfilledValue(pageSettled)
        const dashboardResult = fulfilledValue(dashboardSettled)

        if (!pageResult && !dashboardResult) {
          throw firstRejectedReason(pageSettled, dashboardSettled) ?? new Error("Papers request failed")
        }

        const fallbackPapers = dashboardResult ? [] : fallbackPaperQuery(papers, { query, period, sort, topic, from, to, has: featureFilters })
        const pagePapers = pageResult ? publicPapers(pageResult.papers) : null
        const dashboardResultPapers = dashboardResult ? publicPapers(dashboardResult.papers) : null
        const nextDashboardPapers = dashboardResultPapers ?? fallbackPapers
        const nextVisiblePapers =
          pagePapers ??
          nextDashboardPapers.slice(pageOffset, pageOffset + PAPER_PAGE_SIZE)
        const nextTotalCount = totalCountForPublicResults({
          pageResult,
          pagePapers,
          dashboardResult,
          dashboardResultPapers,
          fallbackPapers: nextDashboardPapers
        })
        const nextNotices = [
          ...(pageResult?.notices ?? []),
          ...(dashboardResult?.notices ?? []),
          ...rejectedNotices(pageSettled, dashboardSettled, locale)
        ].map((notice) => localizedResearchNotice(notice, locale) ?? notice)

        setVisiblePapers(nextVisiblePapers)
        setDashboardPapers(nextDashboardPapers)
        setPaperTotalCount(nextTotalCount)
        setLatestPublishedAt(dashboardResult?.latestPublishedAt ?? pageResult?.latestPublishedAt ?? latestPaperTimestamp(papers))
        setNotices(nextNotices)
        setHasDataIssue(pageSettled.status === "rejected" || dashboardSettled.status === "rejected")
        setDataContext({
          source: dashboardResult?.source ?? pageResult?.source ?? (nextDashboardPapers.length ? "cache" : undefined),
          collectedAt: dashboardResult?.collectedAt ?? pageResult?.collectedAt
        })

        const nextTotalPages = Math.max(1, Math.ceil(nextTotalCount / PAPER_PAGE_SIZE))
        if (nextTotalCount > 0 && page > nextTotalPages) {
          updatePage(nextTotalPages)
        }
      })
      .catch(() => {
        if (!cancelled) {
          const fallbackPapers = fallbackPaperQuery(papers, { query, period, sort, topic, from, to, has: featureFilters })
          setVisiblePapers(fallbackPapers.slice(pageOffset, pageOffset + PAPER_PAGE_SIZE))
          setDashboardPapers(fallbackPapers)
          setPaperTotalCount(fallbackPapers.length)
          setNotices([t(papersCopy.apiUnavailableCache, locale)])
          setHasDataIssue(true)
          setDataContext({
            source: fallbackPapers.length ? "cache" : "empty",
            collectedAt: latestPaperTimestamp(fallbackPapers)
          })
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false)
          setLoaded(true)
        }
      })
    return () => {
      cancelled = true
    }
  }, [featureFilters, locale, page, pageOffset, papers, period, query, sort, topic, from, to, updatePage])

  const returnParams = new URLSearchParams(searchText)
  returnParams.delete("paper")
  const returnTo = returnParams.size ? `${pathname}?${returnParams}` : pathname
  const [restoreOnMount] = useState(() => hasSavedPaperScroll(returnTo))
  const scrollPending = useRef(restoreOnMount)
  useEffect(() => {
    if (!loaded || isLoading || !scrollPending.current) return
    const frame = requestAnimationFrame(() => {
      scrollPending.current = false
      restorePaperScroll(returnTo)
    })
    return () => cancelAnimationFrame(frame)
  }, [loaded, isLoading, returnTo])

  function previewPaper(paper: Paper) {
    updateQuery({ paper: paper.id })
  }

  function updatePeriod(nextPeriod: PaperPeriod) {
    updateQuery({ period: nextPeriod === "all" ? null : nextPeriod, from: null, to: null, page: null, paper: null })
  }

  function periodHref(nextPeriod: PaperPeriod) {
    const nextParams = new URLSearchParams(searchText)
    nextParams.delete("paper")
    nextParams.delete("page")
    if (nextPeriod === "all") {
      nextParams.delete("period")
    } else {
      nextParams.set("period", nextPeriod)
    }
    const nextQuery = nextParams.toString()
    return nextQuery ? `${pathname}?${nextQuery}` : pathname
  }

  function updateSort(nextSort: PaperSort) {
    updateQuery({ sort: nextSort === "trending" && !query ? null : nextSort, page: null, paper: null })
  }

  function updateFeatureFilter(filter: PaperFeatureFilter) {
    const nextFilters = featureFilters.includes(filter)
      ? featureFilters.filter((item) => item !== filter)
      : [...featureFilters, filter]
    updateQuery({ has: serializePaperFeatureFilters(nextFilters) || null, page: null, paper: null })
  }

  function closeDrawer() {
    updateQuery({ paper: null })
  }

  function closeDrawerHref() {
    const nextParams = new URLSearchParams(searchText)
    nextParams.delete("paper")
    const nextQuery = nextParams.toString()
    return nextQuery ? `${pathname}?${nextQuery}` : pathname
  }

  if (renderView) {
    return renderView({
      locale, papers: visiblePapers, metrics: portalMetrics, query, period, sort,
      topic, from, to, question, view, returnTo, catalog: papers,
      source: dataContext.source, collectedAt: dataContext.collectedAt, latestPublishedAt, hasDataIssue,
      filters: featureFilters, isLoading,
      emptyDescription: paperEmptyDescription({ query, hasFilters: featureFilters.length > 0, notices, hasDataIssue, locale }),
      notice: notices.length || hasDataIssue ? <ResearchStatusNotice notices={notices} context={dataContext} locale={locale} /> : null,
      pagination: <PaperPagination currentPage={page} totalCount={paperTotalCount} pageSize={PAPER_PAGE_SIZE} visibleCount={visiblePapers.length} locale={locale} onPageChange={updatePage} />,
      drawer: <PaperDetailDrawer paper={selectedPaper} paperId={selectedPaperId} locale={locale} open={Boolean(selectedPaperId)} closeHref={closeDrawerHref()} returnTo={returnTo} onOpenChange={(open) => { if (!open) closeDrawer() }} />,
      onSearch: (value) => updateQuery({ q: value.trim(), ...(!value.trim() && sort === "relevance" ? { sort: null } : {}), page: null, paper: null }),
      onTopicChange: (value) => updateQuery({ topic: value, page: null, paper: null }),
      onDateRangeChange: (start, end) => updateQuery({ from: start, to: end, period: null, page: null, paper: null }),
      onQuestionClear: () => updateQuery({ question: null }),
      onViewChange: (value) => {
        updateQuery({ view: value === "discover" ? null : value, paper: null })
        if (window.scrollY > 0) window.scrollTo({ top: 0, behavior: "instant" })
      },
      onPeriodChange: updatePeriod,
      onSortChange: updateSort,
      onFilterToggle: updateFeatureFilter,
      onReset: () => updateQuery({ q: null, has: null, topic: null, from: null, to: null, period: null, sort: null, page: null, paper: null }),
      onPreview: previewPaper
    })
  }

  return (
    <div className="space-y-0">
      <PapersMicrobar
        items={[{ label: "Trending" }]}
        meta={t(papersCopy.frontendView, locale)}
        locale={locale}
      />
      <PapersHero
        title={t(papersCopy.researchPapers, locale)}
        subtitle={t(papersCopy.researchSubtitle, locale)}
        variant="editorial"
        stats={[
          { label: t(papersCopy.papers, locale), value: portalMetrics.paperCount },
          { label: t(papersCopy.tasks, locale), value: portalMetrics.taskCount },
          { label: t(papersCopy.repositories, locale), value: portalMetrics.repositoryCount }
        ]}
        aside={<PaperPeriodTabs value={period} locale={locale} hrefForPeriod={periodHref} onChange={updatePeriod} fullWidth />}
      />
      <div className="mt-3 border-t border-[#dfe5df] dark:border-border" />
      {notices.length || hasDataIssue ? (
        <ResearchStatusNotice notices={notices} context={dataContext} locale={locale} />
      ) : null}
      <div className="mt-6 grid gap-6 xl:grid-cols-[14rem_minmax(0,1fr)] 2xl:grid-cols-[15rem_minmax(0,1fr)]">
        <div className="space-y-3 xl:order-2">
          {isLoading ? (
            <p className="rounded-lg border border-[#dfe5df] bg-white/60 px-4 py-3 text-sm text-[#334155]/60 dark:border-border dark:bg-card/40 dark:text-muted-foreground">
              {t(papersCopy.updatingPapers, locale)}
            </p>
          ) : null}
          <PaperFeatureFilterBar selected={featureFilters} locale={locale} onToggle={updateFeatureFilter} />
          <PaperStream
            papers={visiblePapers}
            locale={locale}
            title={locale === "zh" ? "论文流" : "Paper feed"}
            emptyDescription={paperEmptyDescription({ query, hasFilters: featureFilters.length > 0, notices, hasDataIssue, locale })}
            sort={sort}
            onSortChange={updateSort}
            onPreview={previewPaper}
          />
          <PaperPagination
            currentPage={page}
            totalCount={paperTotalCount}
            pageSize={PAPER_PAGE_SIZE}
            visibleCount={visiblePapers.length}
            locale={locale}
            onPageChange={updatePage}
          />
        </div>
        <PapersDomainSidebar
          className="xl:order-1"
          methodAreas={methodAreas}
          topTasks={topDomains}
          dashboardPapers={dashboardPapers}
          locale={locale}
        />
      </div>
      <PaperDetailDrawer
        paper={selectedPaper}
        paperId={selectedPaperId}
        locale={locale}
        open={Boolean(selectedPaperId)}
        closeHref={closeDrawerHref()}
        onOpenChange={(open) => {
          if (!open) {
            closeDrawer()
          }
        }}
      />
    </div>
  )
}

function fulfilledValue(result: PromiseSettledResult<PaperListResult>): PaperListResult | null {
  return result.status === "fulfilled" ? result.value : null
}

function firstRejectedReason(...results: PromiseSettledResult<PaperListResult>[]): unknown {
  return results.find((result) => result.status === "rejected")?.reason
}

function rejectedNotices(
  pageResult: PromiseSettledResult<PaperListResult>,
  dashboardResult: PromiseSettledResult<PaperListResult>,
  locale: Locale
) {
  const notices: string[] = []
  if (pageResult.status === "rejected") {
    notices.push(locale === "zh" ? "当前列表已从可用的真实论文数据恢复。" : "The list recovered from available verified paper data.")
  }
  if (dashboardResult.status === "rejected") {
    notices.push(t(papersCopy.apiUnavailableCache, locale))
  }
  return notices
}

function paperEmptyDescription({
  query,
  hasFilters,
  notices,
  hasDataIssue,
  locale
}: {
  query: string
  hasFilters: boolean
  notices: string[]
  hasDataIssue: boolean
  locale: Locale
}) {
  if (query.trim() || hasFilters) {
    return locale === "zh"
      ? "当前搜索或筛选下没有公开论文匹配。可以清空搜索，或稍后在论文入库完成后重试。"
      : "No public papers match the current search or filters. Clear the search, or retry after paper ingest finishes."
  }
  if (hasDataIssue || notices.some((notice) => /no .*paper|unavailable|cache|artifact|暂无|不可用/i.test(notice))) {
    return locale === "zh"
      ? "当前没有可展示的真实论文数据。请稍后刷新，或确认论文入库已经完成。"
      : "No verified paper data is available yet. Refresh after paper ingest completes."
  }
  return locale === "zh"
    ? "当前还没有可展示的公开论文。"
    : "There are no public papers to display yet."
}

function totalCountForPublicResults({
  pageResult,
  pagePapers,
  dashboardResult,
  dashboardResultPapers,
  fallbackPapers
}: {
  pageResult: PaperListResult | null
  pagePapers: Paper[] | null
  dashboardResult: PaperListResult | null
  dashboardResultPapers: Paper[] | null
  fallbackPapers: Paper[]
}) {
  if (dashboardResult && dashboardResultPapers) {
    return totalCountForFilteredResult(dashboardResult, dashboardResultPapers)
  }
  if (!pageResult || !pagePapers) {
    return fallbackPapers.length
  }
  return totalCountForFilteredResult(pageResult, pagePapers)
}

function totalCountForFilteredResult(result: PaperListResult, publicPapers: Paper[]) {
  if (result.total_count === result.papers.length && publicPapers.length !== result.papers.length) {
    return publicPapers.length
  }
  return Math.max(result.total_count, publicPapers.length)
}

function parsePeriod(value: string | null): PaperPeriod {
  return value === "daily" || value === "weekly" || value === "monthly" || value === "all" ? value : "all"
}

function parseSort(value: string | null, hasQuery = false): PaperSort {
  return value === "newest" || value === "most_cited" || value === "trending" || value === "relevance" ? value : hasQuery ? "relevance" : "trending"
}

function parsePage(value: string | null): number {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1
}

function fallbackPaperQuery(
  papers: Paper[],
  { query, ...options }: PaperQuery & { query: string }
) {
  return queryPapers(papers, { ...options, q: query }).papers
}

function publicPapers(papers: Paper[]) {
  return papers.filter((paper) => paper.isPublished !== false)
}

function PaperFeatureFilterBar({
  selected,
  locale,
  onToggle
}: {
  selected: PaperFeatureFilter[]
  locale: Locale
  onToggle: (filter: PaperFeatureFilter) => void
}) {
  return (
    <div className="flex gap-2 overflow-x-auto rounded-xl border border-[#dfe5df] bg-white/60 p-2 dark:border-border dark:bg-card/40" aria-label={locale === "zh" ? "论文筛选" : "Paper filters"}>
      {paperFeatureFilters.map((filter) => {
        const active = selected.includes(filter)
        return (
          <Button
            key={filter}
            type="button"
            variant={active ? "default" : "outline"}
            size="sm"
            className="h-8 shrink-0 rounded-full px-3 text-xs shadow-none"
            aria-pressed={active}
            onClick={() => onToggle(filter)}
          >
            {filterIcon(filter)}
            {filterLabel(filter, locale)}
          </Button>
        )
      })}
    </div>
  )
}

function ResearchStatusNotice({
  notices,
  context,
  locale
}: {
  notices: string[]
  context: PaperDataContext
  locale: Locale
}) {
  const message = [...new Set(notices)].filter(Boolean).join(" ") || t(papersCopy.apiUnavailableCache, locale)
  const source = paperDataSourceLabel(context.source, locale)
  const updated = context.collectedAt ? formatDataUpdatedAt(context.collectedAt, locale) : null
  const meta = [source, updated].filter(Boolean).join(locale === "zh" ? " · " : " · ")

  return (
    <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50/90 px-4 py-3 text-sm leading-6 text-amber-900">
      <p>{message}</p>
      {meta ? <p className="mt-1 text-xs text-amber-900/70">{meta}</p> : null}
    </div>
  )
}

function filterIcon(filter: PaperFeatureFilter) {
  if (filter === "pdf") return <FileText className="size-3.5" aria-hidden="true" />
  if (filter === "code") return <Github className="size-3.5" aria-hidden="true" />
  if (filter === "benchmark") return <BarChart3 className="size-3.5" aria-hidden="true" />
  return <Quote className="size-3.5" aria-hidden="true" />
}

function filterLabel(filter: PaperFeatureFilter, locale: Locale) {
  return translate(locale, `papers.paperFilter.${filter}` as TranslationKey) || fallbackFilterLabel(filter, locale)
}

function fallbackFilterLabel(filter: PaperFeatureFilter, locale: Locale) {
  const labels: Record<PaperFeatureFilter, Record<Locale, string>> = {
    pdf: { zh: "有 PDF", en: "PDF" },
    code: { zh: "有代码", en: "Code" },
    benchmark: { zh: "有评测", en: "Benchmarks" },
    citation: { zh: "有引用", en: "Citations" }
  }
  return labels[filter][locale]
}

function paperDataSourceLabel(source: string | undefined, locale: Locale) {
  if (source === "backend") {
    return locale === "zh" ? "来源：实时论文索引" : "Source: live paper index"
  }
  if (source === "artifact") {
    return locale === "zh" ? "来源：Paper Radar 产物" : "Source: Paper Radar artifacts"
  }
  if (source === "empty") {
    return locale === "zh" ? "来源：暂无可用真实论文数据" : "Source: no verified paper data yet"
  }
  return locale === "zh" ? "来源：已缓存真实论文数据" : "Source: verified cached papers"
}

function formatDataUpdatedAt(value: string, locale: Locale) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) {
    return null
  }
  return `${locale === "zh" ? "最近更新" : "Updated"}：${new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-US", {
    year: "numeric",
    month: "short",
    day: "numeric"
  }).format(date)}`
}

function latestPaperTimestamp(papers: Paper[]) {
  const timestamps = papers.map((paper) => new Date(paper.publishedAt).getTime()).filter((value) => Number.isFinite(value))
  return timestamps.length ? new Date(Math.max(...timestamps)).toISOString() : undefined
}

function PaperPagination({
  currentPage,
  totalCount,
  pageSize,
  visibleCount,
  locale,
  onPageChange
}: {
  currentPage: number
  totalCount: number
  pageSize: number
  visibleCount: number
  locale: Locale
  onPageChange: (page: number) => void
}) {
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize))
  if (totalPages <= 1) {
    return null
  }

  const safePage = Math.min(Math.max(currentPage, 1), totalPages)
  const start = totalCount === 0 || visibleCount === 0 ? 0 : Math.min((safePage - 1) * pageSize + 1, totalCount)
  const end = visibleCount === 0 ? 0 : Math.min(start + visibleCount - 1, totalCount)
  const pages = paginationRange(safePage, totalPages)
  const previousLabel = locale === "zh" ? "上一页" : "Previous page"
  const nextLabel = locale === "zh" ? "下一页" : "Next page"
  const rangeLabel =
    locale === "zh"
      ? `第 ${start}-${end} 篇，共 ${totalCount} 篇`
      : `${start}-${end} of ${totalCount} papers`

  return (
    <nav
      aria-label={locale === "zh" ? "论文分页" : "Paper pagination"}
      className="flex flex-col gap-3 rounded-xl border border-[#dfe5df] bg-white/60 px-4 py-4 sm:flex-row sm:items-center sm:justify-between dark:border-border dark:bg-card/40"
    >
      <p className="text-xs font-medium text-[#334155]/62 dark:text-muted-foreground">{rangeLabel}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="size-8 rounded-lg bg-white shadow-none dark:bg-card"
          aria-label={previousLabel}
          disabled={safePage <= 1}
          onClick={() => onPageChange(safePage - 1)}
        >
          <ChevronLeft className="size-4" aria-hidden="true" />
        </Button>
        {pages.map((item) =>
          typeof item === "number" ? (
            <Button
              key={item}
              type="button"
              variant={item === safePage ? "default" : "outline"}
              size="sm"
              className="h-8 min-w-8 rounded-lg px-2 shadow-none"
              aria-label={locale === "zh" ? `第 ${item} 页` : `Page ${item}`}
              aria-current={item === safePage ? "page" : undefined}
              onClick={() => onPageChange(item)}
            >
              {item}
            </Button>
          ) : (
            <span key={item} className="flex h-8 min-w-8 items-center justify-center text-xs text-[#334155]/45">
              ...
            </span>
          )
        )}
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="size-8 rounded-lg bg-white shadow-none dark:bg-card"
          aria-label={nextLabel}
          disabled={safePage >= totalPages}
          onClick={() => onPageChange(safePage + 1)}
        >
          <ChevronRight className="size-4" aria-hidden="true" />
        </Button>
      </div>
    </nav>
  )
}

function paginationRange(currentPage: number, totalPages: number): Array<number | string> {
  const pages = new Set<number>([1, totalPages])
  for (let page = currentPage - 1; page <= currentPage + 1; page += 1) {
    if (page >= 1 && page <= totalPages) {
      pages.add(page)
    }
  }
  const sorted = Array.from(pages).sort((left, right) => left - right)
  const range: Array<number | string> = []
  for (const page of sorted) {
    const previous = range[range.length - 1]
    if (typeof previous === "number" && page - previous > 1) {
      range.push(`ellipsis-${previous}-${page}`)
    }
    range.push(page)
  }
  return range
}
