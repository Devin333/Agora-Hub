"use client"

import { useEffect, useMemo, useState, type FormEvent } from "react"
import Link from "next/link"
import { ArrowRight, BarChart3, BookMarked, BookOpen, Check, FileText, Github, GitCompareArrows, Layers3, Quote, Search, WandSparkles, X } from "lucide-react"
import { TrendingPapersPage, type PapersDiscoveryViewModel } from "@/components/papers/trending-papers-page"
import { paperFeatureFilters, type PaperFeatureFilter } from "@/lib/papers/filters"
import { formatPaperDate } from "@/lib/papers/format"
import type { Paper, PaperPeriod, PaperSort } from "@/lib/papers/types"
import { useUiStore } from "@/stores/ui-store"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import { PaperDiscoveryRow } from "./paper-discovery-row"
import { PaperWorkspaceView } from "./paper-workspace-view"
import styles from "./papers-design-demo.module.css"
import { PortalAccountControl } from "@/components/auth/portal-account-control"
import { useResearchReturnContext } from "@/lib/auth/research-return-context"

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
const topicNames: Record<string, { zh: string; en: string }> = {
  "cs.AI": { zh: "人工智能", en: "Artificial intelligence" },
  "cs.LG": { zh: "机器学习", en: "Machine learning" },
  "cs.CL": { zh: "自然语言处理", en: "Language processing" },
  "cs.CV": { zh: "计算机视觉", en: "Computer vision" },
  "cs.RO": { zh: "机器人", en: "Robotics" },
  "cs.MA": { zh: "多智能体", en: "Multi-agent systems" },
  "cs.IR": { zh: "信息检索", en: "Information retrieval" },
  "cs.SE": { zh: "软件工程", en: "Software engineering" },
  "cs.HC": { zh: "人机交互", en: "Human-computer interaction" },
  "cs.CR": { zh: "信息安全", en: "Security" },
  "stat.ML": { zh: "统计学习", en: "Statistical learning" }
}

export function PapersDesignDemoPage({ papers }: { papers: Paper[] }) {
  const locale = useUiStore((state) => state.locale)
  const topics = useMemo(() => {
    const counts = new Map<string, number>()
    for (const paper of papers.filter((item) => item.isPublished !== false)) {
      for (const tag of new Set(paper.tags)) counts.set(tag, (counts.get(tag) ?? 0) + 1)
    }
    return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 8)
  }, [papers])
  return <TrendingPapersPage locale={locale} papers={papers} renderView={(model) => <PapersDiscoveryView model={model} topics={topics} />} />
}

export function PapersDiscoveryView({ model, topics }: { model: PapersDiscoveryViewModel; topics: Array<[string, number]> }) {
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
  const hasFilters = Boolean(query || filters.length || model.topic || hasTime || model.sort !== "trending")
  const latest = model.latestPublishedAt ? formatPaperDate(model.latestPublishedAt, locale) : null
  const timeBeyondCorpus = isTimeBeyondCorpus(model)
  const stale = model.latestPublishedAt && Date.now() - Date.parse(model.latestPublishedAt) > 30 * 86400000
  function search(event: FormEvent<HTMLFormElement>) { event.preventDefault(); model.onSearch(draft) }
  const chips = [
    ...(query ? [{ key: "query", label: query, remove: () => model.onSearch("") }] : []),
    ...(model.topic ? [{ key: "topic", label: topicNames[model.topic]?.[locale] ?? model.topic, remove: () => model.onTopicChange("") }] : []),
    ...(hasTime ? [{ key: "time", label: model.from || model.to ? `${model.from || "…"} – ${model.to || "…"}` : periods.find((period) => period.value === model.period)![locale], remove: () => model.onPeriodChange("all") }] : [])
  ]

  return <div className={styles.page}>
    <header className={styles.header}><nav className={styles.headerInner} aria-label={zh ? "主导航" : "Main navigation"}>
      <Link href="/design-demo" className={styles.brand} aria-label="Agora AI"><span className={styles.brandIcon}><WandSparkles size={19} /></span><span>Agora<span className={styles.accent}>AI</span></span></Link>
      <div className={styles.navigation}><Link href="/design-demo">{zh ? "首页" : "Home"}</Link><Link href="/design-demo/papers" aria-current="page">{zh ? "论文研究" : "Papers"}</Link><Link href="/projects">{zh ? "项目雷达" : "Projects"}</Link><Link href="/community">{zh ? "社区信号" : "Community"}</Link></div>
      <div className={styles.headerActions}><PortalAccountControl /><Link href="/design-demo#workspace" className={styles.newResearch}><WandSparkles size={16} />{zh ? "开始研究" : "New research"}</Link></div>
    </nav></header>
    <main className={styles.main}>
      <section className={styles.intro} aria-labelledby="papers-title"><h1 id="papers-title">{zh ? "论文研究" : "Research papers"}<span className={styles.titleDot}>.</span></h1><span className={styles.introCaption}>{zh ? "发现论文，连接研究线索。" : "Discover papers. Connect ideas."}</span></section>
      <div className={styles.workspace}>
        <aside className={styles.sidebar} aria-label={zh ? "研究分类" : "Research categories"}>
          <div className={styles.viewNav}>
            <button type="button" aria-pressed={view === "discover"} onClick={() => model.onViewChange("discover")}><BookOpen size={18} />{zh ? "发现论文" : "Discover"}</button>
            <button type="button" aria-pressed={view === "reading"} onClick={() => model.onViewChange("reading")}><BookMarked size={18} />{zh ? "阅读列表" : "Reading list"}<span>{savedIds.length}</span></button>
            <button type="button" aria-pressed={view === "compare"} onClick={() => model.onViewChange("compare")}><GitCompareArrows size={18} />{zh ? "论文对比" : "Compare"}<span>{compare.length}</span></button>
          </div>
          {view === "discover" && <>
            <h2 className={styles.topicHeading}><Layers3 size={17} />{zh ? "研究领域" : "Research topics"}</h2>
            <p className={styles.countScope}>{zh ? "全库篇数" : "Catalog counts"}</p>
            <div className={styles.topics}><button type="button" aria-pressed={!model.topic} onClick={() => model.onTopicChange("")}><span>{zh ? "全部领域" : "All topics"}</span></button>{topics.map(([tag, count]) => <button type="button" key={tag} aria-label={`${topicNames[tag]?.[locale] ?? tag}, ${tag}, ${count} ${zh ? "篇论文" : "papers"}`} aria-pressed={model.topic === tag} onClick={() => model.onTopicChange(model.topic === tag ? "" : tag)}><span>{topicNames[tag]?.[locale] ?? tag}{topicNames[tag] && <small>{tag}</small>}</span><span>{count}</span></button>)}</div>
            <div className={styles.sidebarLinks}><Link href="/papers/methods">{zh ? "研究方法目录" : "Methods"}<ArrowRight size={15} /></Link><Link href="/papers/tasks">{zh ? "研究任务目录" : "Tasks"}<ArrowRight size={15} /></Link></div>
          </>}
        </aside>
        {view === "discover" ? <section className={styles.results} aria-labelledby="paper-results-title" aria-busy={model.isLoading}>
          {model.question && <div className={styles.questionContext}><span><strong>{zh ? "研究问题" : "Research question"}</strong>{model.question}</span><button type="button" aria-label={zh ? "移除研究问题" : "Dismiss research question"} title={zh ? "移除研究问题" : "Dismiss research question"} onClick={model.onQuestionClear}><X size={16} /></button></div>}
          <form onSubmit={search} role="search" className={styles.search}><Search size={20} /><span className={styles.searchMode}>{zh ? "关键词" : "Keywords"}</span><input aria-label={zh ? "搜索论文" : "Search papers"} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={zh ? "论文标题、作者、研究方向..." : "Title, author, research topic..."} />{draft && <button type="button" className={styles.clearSearch} aria-label={zh ? "清空搜索" : "Clear search"} title={zh ? "清空搜索" : "Clear search"} onClick={() => { setDraft(""); model.onSearch("") }}><X size={18} /></button>}<button className={styles.submit} type="submit" aria-label={zh ? "搜索" : "Search"} title={zh ? "搜索" : "Search"}><ArrowRight size={20} /></button></form>
          <div className={styles.filterBar}><div className={styles.features} role="group" aria-label={zh ? "论文筛选" : "Paper filters"}>{paperFeatureFilters.map((filter) => { const option = featureOptions[filter]; const Icon = option.icon; const active = filters.includes(filter); return <button type="button" key={filter} aria-pressed={active} onClick={() => model.onFilterToggle(filter)}><Icon size={15} />{option[locale]}{active && <Check size={13} />}</button> })}</div><label className={styles.period}><select aria-label={zh ? "发布时间" : "Publication period"} value={model.period} onChange={(event) => model.onPeriodChange(event.target.value as PaperPeriod)}>{periods.map((period) => <option key={period.value} value={period.value}>{period[locale]}</option>)}</select></label><DateRange model={model} /></div>
          {!!chips.length && <div className={styles.activeFilters} aria-label={zh ? "已选筛选" : "Active filters"}>{chips.map((chip) => <button key={chip.key} type="button" onClick={chip.remove} aria-label={`${zh ? "移除筛选" : "Remove filter"} ${chip.label}`}><span>{chip.label}</span><X size={13} /></button>)}{hasFilters && <button type="button" className={styles.reset} onClick={model.onReset}>{zh ? "重置" : "Reset"}</button>}</div>}
          {!chips.length && hasFilters && <button type="button" className={styles.inlineReset} onClick={model.onReset}>{zh ? "重置" : "Reset"}</button>}
          <div className={styles.dataStatus} data-warning={Boolean(stale || model.hasDataIssue)} role="status"><span className={styles.statusDot} /><span>{model.hasDataIssue ? (zh ? "实时数据暂不可用，显示可用缓存" : "Live data unavailable; showing available cache") : model.source === "cache" ? (zh ? "缓存数据" : "Cached data") : (zh ? "已发布论文" : "Published papers")}{latest ? ` · ${zh ? "最新收录论文发表于" : "Latest publication"} ${latest}` : ""}{stale ? (zh ? " · 近期数据可能尚未收录" : " · Recent papers may not be indexed") : ""}</span>{model.collectedAt && <span className={styles.collectionTime} title={model.collectedAt}>{zh ? "数据更新" : "Updated"} {formatPaperDate(model.collectedAt, locale)}</span>}</div>
          <div className={styles.resultsHeader}><div className={styles.resultHeading}><h2 id="paper-results-title">{query || model.topic ? (zh ? "搜索结果" : "Search results") : (zh ? "发现论文" : "Discover papers")}</h2><span aria-live="polite">{model.isLoading ? (zh ? "更新中..." : "Updating...") : `${metrics.paperCount.toLocaleString()} ${zh ? "篇论文" : "papers"}`}</span></div><div className={styles.sortTabs} role="group" aria-label={zh ? "论文排序" : "Paper sort"}>{sorts.filter((sort) => sort.value !== "relevance" || query).map((sort) => <button key={sort.value} type="button" aria-pressed={model.sort === sort.value} onClick={() => model.onSortChange(sort.value)}>{sort[locale]}</button>)}</div></div>
          <div className={styles.paperList}>{papers.map((paper, index) => <PaperDiscoveryRow key={paper.id} paper={paper} locale={locale} onPreview={model.onPreview} renderPdfPreview={index < 3} returnTo={model.returnTo} />)}</div>
          {!papers.length && !model.isLoading && <div className={styles.empty}><Search size={32} /><h3>{zh ? "没有找到论文" : "No papers found"}</h3><p>{timeBeyondCorpus ? (zh ? `当前最新收录论文发表于 ${latest}，所选时间段暂无数据。` : `The latest available publication is ${latest}; no data covers this period.`) : model.emptyDescription}</p>{query && <p>{zh ? "当前按关键词匹配。" : "Matching keywords."}</p>}{hasTime && <button type="button" onClick={() => model.onPeriodChange("all")}>{zh ? "查看全部时间" : "Show all time"}</button>}{hasFilters && <button type="button" onClick={model.onReset}>{zh ? "清除筛选" : "Clear filters"}</button>}</div>}
          <div className={styles.pagination}>{model.pagination}</div>
        </section> : <PaperWorkspaceView view={view} ids={view === "reading" ? savedIds : compare} catalog={model.catalog} locale={locale} returnTo={model.returnTo} onPreview={model.onPreview} onDiscover={() => model.onViewChange("discover")} />}
      </div>
    </main>
    {compare.length > 0 && view !== "compare" && <div className={styles.compareTray}><GitCompareArrows size={18} /><span>{zh ? `已选 ${compare.length} 篇论文` : `${compare.length} papers selected`}</span><button type="button" className={styles.trayClear} onClick={() => compare.forEach((id) => remove("compare", id))}>{zh ? "清空" : "Clear"}</button><button type="button" onClick={() => model.onViewChange("compare")}>{zh ? "查看对比" : "Compare"}<ArrowRight size={16} /></button></div>}
    <div className={styles.drawer}>{model.drawer}</div>
  </div>
}

function DateRange({ model }: { model: PapersDiscoveryViewModel }) {
  const [from, setFrom] = useState(model.from)
  const [to, setTo] = useState(model.to)
  const [open, setOpen] = useState(false)
  const zh = model.locale === "zh"
  useEffect(() => { setFrom(model.from); setTo(model.to) }, [model.from, model.to])
  return <div className={styles.dateRange}><button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{zh ? "日期范围" : "Date range"}</button>{open && <form className={styles.datePopover} onSubmit={(event) => { event.preventDefault(); model.onDateRangeChange(from, to); setOpen(false) }}><label>{zh ? "开始日期" : "From"}<input type="date" value={from} max={to || undefined} onChange={(event) => setFrom(event.target.value)} /></label><label>{zh ? "结束日期" : "To"}<input type="date" value={to} min={from || undefined} onChange={(event) => setTo(event.target.value)} /></label><div><button type="button" onClick={() => setOpen(false)}>{zh ? "取消" : "Cancel"}</button><button type="submit" disabled={!from && !to}>{zh ? "应用" : "Apply"}</button></div></form>}</div>
}

function isTimeBeyondCorpus(model: PapersDiscoveryViewModel) {
  if (!model.latestPublishedAt) return false
  const days = model.period === "daily" ? 1 : model.period === "weekly" ? 7 : model.period === "monthly" ? 30 : 0
  const start = model.from ? Date.parse(model.from) : days ? Date.now() - days * 86400000 : 0
  return start > Date.parse(model.latestPublishedAt)
}
