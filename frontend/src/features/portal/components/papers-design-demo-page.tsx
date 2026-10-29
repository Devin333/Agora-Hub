"use client"

import { useEffect, useMemo, useState, type FormEvent } from "react"
import { ArrowRight, BarChart3, BookMarked, BookOpen, Check, FileText, Github, GitCompareArrows, Quote, Search, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { TrendingPapersPage, type PapersDiscoveryViewModel } from "@/components/papers/trending-papers-page"
import { paperFeatureFilters, type PaperFeatureFilter } from "@/lib/papers/filters"
import { formatPaperDate } from "@/lib/papers/format"
import type { Paper, PaperPeriod, PaperSort } from "@/lib/papers/types"
import { cn } from "@/lib/utils"
import { useUiStore } from "@/stores/ui-store"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import { PaperDiscoveryRow } from "./paper-discovery-row"
import { PaperWorkspaceView } from "./paper-workspace-view"
import { filterChipClassName, nativeSelectClassName, PaperPortalShell, searchBoxClassName, sidebarRowClassName, sidebarRowMetaClassName } from "./paper-portal-shell"
import { PaperCategorySidebar } from "./paper-category-sidebar"
import { buildPaperCategories, type PaperCategoryDefinitions, type PaperCategoryGroups } from "@/lib/papers/discovery-categories"
import { canonicalRefSlug } from "@/lib/papers/query"
import { useResearchReturnContext } from "@/lib/auth/research-return-context"
import { ResearchMaterialContext } from "@/components/research/research-material-context"

const featureOptions = {
  pdf: { zh: "有 PDF", en: "PDF available", icon: FileText },
  code: { zh: "有代码", en: "With code", icon: Github },
  benchmark: { zh: "有评测", en: "Evaluated", icon: BarChart3 },
  citation: { zh: "有引用", en: "Cited", icon: Quote }
} satisfies Record<PaperFeatureFilter, { zh: string; en: string; icon: typeof FileText }>
const periods: Array<{ value: PaperPeriod; zh: string; en: string }> = [
  { value: "all", zh: "全部时间", en: "All time" },
  { value: "daily", zh: "近 24 小时", en: "Last 24 hours" },
  { value: "weekly", zh: "近 7 天", en: "Last 7 days" },
  { value: "monthly", zh: "近 30 天", en: "Last 30 days" }
]
const sorts: Array<{ value: PaperSort; zh: string; en: string }> = [
  { value: "relevance", zh: "相关度", en: "Relevance" },
  { value: "trending", zh: "趋势", en: "Trending" },
  { value: "newest", zh: "最新", en: "Newest" },
  { value: "most_cited", zh: "高引用", en: "Most cited" }
]
export function PapersDesignDemoPage({ papers, taxonomy }: { papers: Paper[]; taxonomy?: PaperCategoryDefinitions }) {
  const locale = useUiStore((state) => state.locale)
  const categories = useMemo(() => buildPaperCategories(papers, taxonomy), [papers, taxonomy])
  return <TrendingPapersPage locale={locale} papers={papers} renderView={(model) => <PapersDiscoveryView model={model} categories={categories} />} />
}

export function PapersDiscoveryView({ model, categories }: { model: PapersDiscoveryViewModel; categories: PaperCategoryGroups }) {
  const { locale, query, metrics, papers, filters, view } = model
  const zh = locale === "zh"
  const [draft, setDraft] = useState(query)
  const readingList = usePaperWorkspaceStore((state) => state.readingList)
  const later = usePaperWorkspaceStore((state) => state.later)
  const compare = usePaperWorkspaceStore((state) => state.compare)
  const remove = usePaperWorkspaceStore((state) => state.removePaper)
  const savedIds = useMemo(() => [...new Set([...readingList, ...later])], [readingList, later])
  useEffect(() => setDraft(query), [query])
  useResearchReturnContext("papers", draft, (saved) => { if (typeof saved === "string") setDraft(saved) })
  const hasTime = model.period !== "all" || Boolean(model.from || model.to)
  const hasFilters = Boolean(query || filters.length || model.topic || model.method || model.task || model.paperType || hasTime || model.sort !== "trending")
  const latest = model.latestPublishedAt ? formatPaperDate(model.latestPublishedAt, locale) : null
  const timeBeyondCorpus = isTimeBeyondCorpus(model)
  const stale = model.latestPublishedAt && Date.now() - Date.parse(model.latestPublishedAt) > 30 * 86400000
  function search(event: FormEvent<HTMLFormElement>) { event.preventDefault(); model.onSearch(draft) }
  const categoryFilters = [
    { key: "topic", value: model.topic, items: categories.topics, remove: () => model.onTopicChange("") },
    { key: "method", value: canonicalRefSlug(model.method, "method"), items: categories.methods, remove: () => model.onMethodChange("") },
    { key: "task", value: canonicalRefSlug(model.task, "task"), items: categories.tasks, remove: () => model.onTaskChange("") }
  ]
  const chips = [
    ...(model.paperType ? [{ key: "paperType", label: zh ? "综述论文" : "Surveys", remove: () => model.onPaperTypeClear?.() }] : []),
    ...(query ? [{ key: "query", label: query, remove: () => model.onSearch("") }] : []),
    ...categoryFilters.filter(item => item.value).map(item => { const category = item.items.find(category => category.value === item.value); return { key: item.key, label: (zh ? category?.nameZh || category?.name : category?.name) || item.value, remove: item.remove } }),
    ...(hasTime ? [{ key: "time", label: model.from || model.to ? `${model.from || "…"} – ${model.to || "…"}` : periods.find((period) => period.value === model.period)![locale], remove: () => model.onPeriodChange("all") }] : [])
  ]

  const viewItems = [
    { value: "discover" as const, icon: BookOpen, label: zh ? "发现论文" : "Discover" },
    { value: "reading" as const, icon: BookMarked, label: zh ? "阅读列表" : "Reading list", count: savedIds.length },
    { value: "compare" as const, icon: GitCompareArrows, label: zh ? "论文对比" : "Compare", count: compare.length }
  ]
  const sidebar = <>
    <div className="grid gap-px">
      {viewItems.map((item) => <button key={item.value} type="button" className={sidebarRowClassName} aria-pressed={view === item.value} onClick={() => model.onViewChange(item.value)}>
        <item.icon size={16} strokeWidth={1.8} className="shrink-0 text-muted-foreground" />{item.label}
        {item.count !== undefined && <span className={sidebarRowMetaClassName}>{item.count}</span>}
      </button>)}
    </div>
    {view === "discover" && <PaperCategorySidebar groups={categories} locale={locale} selected={{ topic: model.topic, method: model.method, task: model.task }} onSelect={(kind, value) => ({ topic: model.onTopicChange, method: model.onMethodChange, task: model.onTaskChange })[kind](value)} />}
  </>
  const overlay = <>
    {compare.length > 0 && view !== "compare" && <CompareTray count={compare.length} zh={zh} onClear={() => compare.forEach((id) => remove("compare", id))} onOpen={() => model.onViewChange("compare")} />}
    {model.drawer}
  </>
  const warning = Boolean(stale || model.hasDataIssue)

  return <PaperPortalShell locale={locale} sidebarLabel={zh ? "研究分类" : "Research categories"} sidebar={sidebar} overlay={overlay}>
    <header className="border-b px-4 py-4 md:px-6" aria-labelledby="papers-title">
      <h1 id="papers-title" className="text-heading2 font-semibold">{zh ? "论文研究" : "Research papers"}</h1>
      <p className="mt-1 text-body2 text-muted-foreground">{zh ? "发现论文，连接研究线索。" : "Discover papers. Connect ideas."}</p>
    </header>
    <div className="px-4 pb-24 pt-5 md:px-6">
      {view === "discover" ? <section className="min-w-0" aria-labelledby="paper-results-title" aria-busy={model.isLoading}>
        <ResearchMaterialContext />
        {model.question && <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border px-4 py-3 text-body2 text-muted-foreground">
          <span className="break-words"><strong className="me-3 font-semibold text-foreground">{zh ? "研究问题" : "Research question"}</strong>{model.question}</span>
          <Button type="button" variant="ghost" size="icon-sm" aria-label={zh ? "移除研究问题" : "Dismiss research question"} title={zh ? "移除研究问题" : "Dismiss research question"} onClick={model.onQuestionClear}><X size={15} strokeWidth={1.8} /></Button>
        </div>}
        <form onSubmit={search} role="search" className={searchBoxClassName}>
          <Search size={16} strokeWidth={1.8} className="shrink-0" />
          <span className="shrink-0 whitespace-nowrap border-e border-border pe-2 text-label">{zh ? "关键词" : "Keywords"}</span>
          <input className="h-full min-w-0 flex-1 bg-transparent text-body1 text-foreground outline-none placeholder:text-muted-foreground" aria-label={zh ? "搜索论文" : "Search papers"} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={zh ? "论文标题、作者、研究方向..." : "Title, author, research topic..."} />
          {draft && <Button type="button" variant="ghost" size="icon-sm" className="rounded-full text-muted-foreground" aria-label={zh ? "清空搜索" : "Clear search"} title={zh ? "清空搜索" : "Clear search"} onClick={() => { setDraft(""); model.onSearch("") }}><X size={15} strokeWidth={1.8} /></Button>}
          <Button type="submit" size="icon" className="rounded-full" aria-label={zh ? "搜索" : "Search"} title={zh ? "搜索" : "Search"}><ArrowRight size={16} strokeWidth={1.8} /></Button>
        </form>
        <div className="flex flex-wrap items-center gap-2 py-3">
          <div className="me-auto flex flex-wrap items-center gap-1.5" role="group" aria-label={zh ? "论文筛选" : "Paper filters"}>{paperFeatureFilters.map((filter) => {
            const option = featureOptions[filter]
            const Icon = option.icon
            const active = filters.includes(filter)
            return <Button type="button" variant="outline" size="sm" key={filter} className="rounded-full px-3 text-muted-foreground aria-pressed:border-transparent aria-pressed:bg-accent aria-pressed:text-foreground" aria-pressed={active} onClick={() => model.onFilterToggle(filter)}><Icon size={14} strokeWidth={1.8} />{option[locale]}{active && <Check size={13} strokeWidth={1.8} />}</Button>
          })}</div>
          <select className={nativeSelectClassName} aria-label={zh ? "发布时间" : "Publication period"} value={model.period} onChange={(event) => model.onPeriodChange(event.target.value as PaperPeriod)}>{periods.map((period) => <option key={period.value} value={period.value}>{period[locale]}</option>)}</select>
          <DateRange model={model} />
        </div>
        {!!chips.length && <div className="mb-3 flex flex-wrap items-center gap-1.5" aria-label={zh ? "已选筛选" : "Active filters"}>
          {chips.map((chip) => <button key={chip.key} type="button" className={filterChipClassName} onClick={chip.remove} aria-label={`${zh ? "移除筛选" : "Remove filter"} ${chip.label}`}><span>{chip.label}</span><X size={12} strokeWidth={1.8} /></button>)}
          {hasFilters && <Button type="button" variant="text" size="xs" onClick={model.onReset}>{zh ? "重置" : "Reset"}</Button>}
        </div>}
        {!chips.length && hasFilters && <Button type="button" variant="text" size="xs" className="mb-3" onClick={model.onReset}>{zh ? "重置" : "Reset"}</Button>}
        <div className={cn("flex flex-wrap items-center gap-2 pb-3 text-label", warning ? "text-warning-text" : "text-muted-foreground")} data-warning={warning} role="status">
          <span className={cn("size-1.5 shrink-0 rounded-full", warning ? "bg-warning" : "bg-success")} />
          <span>{model.hasDataIssue ? (zh ? "实时数据暂不可用，显示可用缓存" : "Live data unavailable; showing available cache") : model.source === "cache" ? (zh ? "缓存数据" : "Cached data") : (zh ? "已发布论文" : "Published papers")}{latest ? ` · ${zh ? "最新收录论文发表于" : "Latest publication"} ${latest}` : ""}{stale ? (zh ? " · 近期数据可能尚未收录" : " · Recent papers may not be indexed") : ""}</span>
          {model.collectedAt && <span className="ms-auto text-faint-foreground" title={model.collectedAt}>{zh ? "数据更新" : "Updated"} {formatPaperDate(model.collectedAt, locale)}</span>}
        </div>
        <div className="mb-3 flex min-h-9 flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-baseline gap-3">
            <h2 id="paper-results-title" className="text-heading3 font-semibold">{query || model.topic || model.method || model.task ? (zh ? "搜索结果" : "Search results") : (zh ? "发现论文" : "Discover papers")}</h2>
            <span className="min-w-12 text-body2 text-muted-foreground" aria-live="polite">{model.isLoading ? (zh ? "更新中..." : "Updating...") : `${metrics.paperCount.toLocaleString()} ${zh ? "篇论文" : "papers"}`}</span>
          </div>
          <div className="inline-flex h-8 shrink-0 items-center rounded-lg bg-muted p-[3px]" role="group" aria-label={zh ? "论文排序" : "Paper sort"}>
            {sorts.filter((sort) => sort.value !== "relevance" || query).map((sort) => <button key={sort.value} type="button" className={segmentClassName} aria-pressed={model.sort === sort.value} onClick={() => model.onSortChange(sort.value)}>{sort[locale]}</button>)}
          </div>
        </div>
        {papers.length > 0 && <div className="divide-y rounded-xl border">{papers.map((paper, index) => <PaperDiscoveryRow key={paper.id} paper={paper} locale={locale} onPreview={model.onPreview} renderPdfPreview={index < 3} returnTo={model.returnTo} />)}</div>}
        {!papers.length && !model.isLoading && <div className="py-16 text-center text-body2 text-muted-foreground">
          <h3 className="text-body1 font-medium text-foreground">{zh ? "没有找到论文" : "No papers found"}</h3>
          <p className="mx-auto mt-1 max-w-[30rem]">{timeBeyondCorpus ? (zh ? `当前最新收录论文发表于 ${latest}，所选时间段暂无数据。` : `The latest available publication is ${latest}; no data covers this period.`) : model.emptyDescription}</p>
          {query && <p className="mt-1">{zh ? "当前按关键词匹配。" : "Matching keywords."}</p>}
          <div className="mt-2 flex justify-center gap-4">
            {hasTime && <Button type="button" variant="link" className="text-body2" onClick={() => model.onPeriodChange("all")}>{zh ? "查看全部时间" : "Show all time"}</Button>}
            {hasFilters && <Button type="button" variant="link" className="text-body2" onClick={model.onReset}>{zh ? "清除筛选" : "Clear filters"}</Button>}
          </div>
        </div>}
        <div className="mt-4">{model.pagination}</div>
      </section> : <PaperWorkspaceView view={view} ids={view === "reading" ? savedIds : compare} catalog={model.catalog} locale={locale} returnTo={model.returnTo} onPreview={model.onPreview} onDiscover={() => model.onViewChange("discover")} />}
    </div>
  </PaperPortalShell>
}

const segmentClassName = "inline-flex h-full items-center rounded-md border border-transparent px-2.5 text-label font-medium text-foreground/60 outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-sm"

function CompareTray({ count, zh, onClear, onOpen }: { count: number; zh: boolean; onClear: () => void; onOpen: () => void }) {
  return <div className="absolute bottom-6 left-1/2 z-30 flex max-w-[calc(100%-2rem)] -translate-x-1/2 items-center gap-3 rounded-full border border-border bg-popover/95 py-1.5 pe-1.5 ps-4 text-body2 text-popover-foreground shadow-md backdrop-blur">
    <GitCompareArrows size={16} strokeWidth={1.8} className="shrink-0 text-muted-foreground" />
    <span className="truncate">{zh ? `已选 ${count} 篇论文` : `${count} papers selected`}</span>
    <Button type="button" variant="text" size="sm" onClick={onClear}>{zh ? "清空" : "Clear"}</Button>
    <Button type="button" size="sm" className="rounded-full px-3" onClick={onOpen}>{zh ? "查看对比" : "Compare"}<ArrowRight size={15} strokeWidth={1.8} /></Button>
  </div>
}

function DateRange({ model }: { model: PapersDiscoveryViewModel }) {
  const [from, setFrom] = useState(model.from)
  const [to, setTo] = useState(model.to)
  const [open, setOpen] = useState(false)
  const zh = model.locale === "zh"
  useEffect(() => { setFrom(model.from); setTo(model.to) }, [model.from, model.to])
  const dateInputClassName = "h-8 w-full min-w-0 rounded-lg border border-input bg-muted px-2 text-body2 text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
  return <div className="relative">
    <Button type="button" variant="text" size="sm" className="text-label" aria-expanded={open} onClick={() => setOpen(!open)}>{zh ? "日期范围" : "Date range"}</Button>
    {open && <form className="absolute end-0 top-9 z-20 grid w-[300px] gap-3 rounded-xl bg-popover p-4 text-body2 text-popover-foreground shadow-md ring-1 ring-foreground/10" onSubmit={(event) => { event.preventDefault(); model.onDateRangeChange(from, to); setOpen(false) }}>
      <label className="grid grid-cols-[72px_minmax(0,1fr)] items-center gap-3 text-muted-foreground">{zh ? "开始日期" : "From"}<input className={dateInputClassName} type="date" value={from} max={to || undefined} onChange={(event) => setFrom(event.target.value)} /></label>
      <label className="grid grid-cols-[72px_minmax(0,1fr)] items-center gap-3 text-muted-foreground">{zh ? "结束日期" : "To"}<input className={dateInputClassName} type="date" value={to} min={from || undefined} onChange={(event) => setTo(event.target.value)} /></label>
      <div className="flex justify-end gap-2"><Button type="button" variant="ghost" size="sm" onClick={() => setOpen(false)}>{zh ? "取消" : "Cancel"}</Button><Button type="submit" size="sm" disabled={!from && !to}>{zh ? "应用" : "Apply"}</Button></div>
    </form>}
  </div>
}

function isTimeBeyondCorpus(model: PapersDiscoveryViewModel) {
  if (!model.latestPublishedAt) return false
  const days = model.period === "daily" ? 1 : model.period === "weekly" ? 7 : model.period === "monthly" ? 30 : 0
  const start = model.from ? Date.parse(model.from) : days ? Date.now() - days * 86400000 : 0
  return start > Date.parse(model.latestPublishedAt)
}
