"use client"

import { useEffect, useMemo, useState, type FormEvent } from "react"
import Link from "next/link"
import { ArrowLeft, ArrowRight, BarChart3, BookOpen, Check, ChevronRight, FileText, Github, Layers3, Library, Quote, Search, SlidersHorizontal, WandSparkles, X } from "lucide-react"
import { PaperRow } from "@/components/papers/paper-row"
import { TrendingPapersPage, type PapersDiscoveryViewModel } from "@/components/papers/trending-papers-page"
import { paperFeatureFilters, type PaperFeatureFilter } from "@/lib/papers/filters"
import type { Paper, PaperPeriod, PaperSort } from "@/lib/papers/types"
import { useUiStore } from "@/stores/ui-store"
import styles from "./papers-design-demo.module.css"

const featureOptions = {
  pdf: { zh: "有 PDF", en: "PDF available", icon: FileText },
  code: { zh: "有代码", en: "With code", icon: Github },
  benchmark: { zh: "有评测", en: "Evaluated", icon: BarChart3 },
  citation: { zh: "有引用", en: "Cited", icon: Quote }
} satisfies Record<PaperFeatureFilter, { zh: string; en: string; icon: typeof FileText }>

const periods: Array<{ value: PaperPeriod; zh: string; en: string }> = [
  { value: "all", zh: "全部时间", en: "All time" },
  { value: "daily", zh: "今日", en: "Today" },
  { value: "weekly", zh: "本周", en: "This week" },
  { value: "monthly", zh: "本月", en: "This month" }
]

const sorts: Array<{ value: PaperSort; zh: string; en: string }> = [
  { value: "trending", zh: "趋势", en: "Trending" },
  { value: "newest", zh: "最新", en: "Newest" },
  { value: "most_cited", zh: "高引用", en: "Most cited" }
]

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
  const { locale, query, metrics, papers, filters } = model
  const zh = locale === "zh"
  const [draft, setDraft] = useState(query)
  useEffect(() => setDraft(query), [query])
  const hasFilters = Boolean(query || filters.length || model.period !== "all" || model.sort !== "trending")

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    model.onSearch(draft)
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <nav className={styles.headerInner} aria-label={zh ? "主导航" : "Main navigation"}>
          <Link href="/design-demo" className={styles.brand} aria-label="Agora AI">
            <span className={styles.brandIcon}><WandSparkles size={19} aria-hidden="true" /></span>
            <span>Agora<span className={styles.accent}>AI</span></span>
          </Link>
          <div className={styles.navigation}>
            <Link href="/design-demo">{zh ? "首页" : "Home"}</Link>
            <Link href="/design-demo/papers" aria-current="page">{zh ? "论文研究" : "Papers"}</Link>
            <Link href="/projects">{zh ? "项目雷达" : "Projects"}</Link>
            <Link href="/community">{zh ? "社区信号" : "Community"}</Link>
          </div>
          <Link href="/design-demo#workspace" className={styles.newResearch}><WandSparkles size={16} aria-hidden="true" />{zh ? "开始研究" : "New research"}</Link>
        </nav>
      </header>

      <main className={styles.main}>
        <nav className={styles.breadcrumb} aria-label={zh ? "面包屑导航" : "Breadcrumb"}>
          <Link href="/design-demo"><ArrowLeft size={14} aria-hidden="true" />{zh ? "研究首页" : "Research home"}</Link>
          <ChevronRight size={13} aria-hidden="true" /><span>{zh ? "论文研究" : "Papers"}</span>
        </nav>

        <section className={styles.intro} aria-labelledby="papers-title">
          <div>
            <h1 id="papers-title">{zh ? "论文研究" : "Research papers"}<span className={styles.titleDot}>.</span></h1>
            <p>{zh ? "发现论文，连接研究线索。" : "Discover papers. Connect ideas."}</p>
          </div>
          <dl className={styles.metrics}>
            <Metric label={zh ? "公开论文" : "Papers"} value={metrics.paperCount} />
            <Metric label={zh ? "研究任务" : "Tasks"} value={metrics.taskCount} />
            <Metric label={zh ? "代码仓库" : "Repositories"} value={metrics.repositoryCount} />
          </dl>
        </section>

        <form onSubmit={search} role="search" className={styles.search}>
          <Search size={21} aria-hidden="true" />
          <input aria-label={zh ? "搜索论文" : "Search papers"} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={zh ? "搜索论文、作者或研究方向..." : "Search papers, authors or research topics..."} />
          {draft && <button type="button" className={styles.clearSearch} aria-label={zh ? "清空搜索" : "Clear search"} title={zh ? "清空搜索" : "Clear search"} onClick={() => { setDraft(""); model.onSearch("") }}><X size={18} aria-hidden="true" /></button>}
          <button className={styles.submit} type="submit" aria-label={zh ? "搜索" : "Search"} title={zh ? "搜索" : "Search"}><ArrowRight size={21} aria-hidden="true" /></button>
        </form>

        <div className={styles.filterBar}>
          <div className={styles.features} role="group" aria-label={zh ? "论文筛选" : "Paper filters"}>
            <SlidersHorizontal size={16} className={styles.filterIcon} aria-hidden="true" />
            {paperFeatureFilters.map((filter) => {
              const option = featureOptions[filter]
              const Icon = option.icon
              const active = filters.includes(filter)
              return <button type="button" key={filter} aria-pressed={active} onClick={() => model.onFilterToggle(filter)}><Icon size={15} aria-hidden="true" />{option[locale]}{active && <Check size={13} aria-hidden="true" />}</button>
            })}
            {hasFilters && <button type="button" className={styles.reset} onClick={model.onReset}>{zh ? "重置" : "Reset"}</button>}
          </div>
          <label className={styles.period}>{zh ? "发布时间" : "Published"}<select aria-label={zh ? "发布时间" : "Publication period"} value={model.period} onChange={(event) => model.onPeriodChange(event.target.value as PaperPeriod)}>{periods.map((period) => <option key={period.value} value={period.value}>{period[locale]}</option>)}</select></label>
        </div>

        <div className={styles.notice}>{model.notice}</div>

        <div className={styles.workspace}>
          <aside className={styles.sidebar} aria-label={zh ? "研究分类" : "Research categories"}>
            <h2><Library size={17} aria-hidden="true" />{zh ? "论文目录" : "Library"}</h2>
            <button type="button" className={styles.allPapers} aria-pressed={!hasFilters} onClick={model.onReset}><BookOpen size={17} aria-hidden="true" />{zh ? "全部论文" : "All papers"}<ChevronRight size={15} aria-hidden="true" /></button>
            <h2 className={styles.topicHeading}><Layers3 size={17} aria-hidden="true" />{zh ? "研究领域" : "Research topics"}</h2>
            <div className={styles.topics}>{topics.map(([tag, count]) => <button type="button" key={tag} aria-label={`${tag}, ${count} ${zh ? "篇论文" : "papers"}`} aria-pressed={query === tag} onClick={() => model.onSearch(query === tag ? "" : tag)}><span>{tag}</span><span>{count}</span></button>)}</div>
            {!topics.length && <p className={styles.noTopics}>{zh ? "暂无领域分类" : "No topics yet"}</p>}
            <div className={styles.sidebarLinks}>
              <Link href="/papers/methods">{zh ? "浏览研究方法" : "Explore methods"}<ArrowRight size={15} aria-hidden="true" /></Link>
              <Link href="/papers/tasks">{zh ? "浏览研究任务" : "Explore tasks"}<ArrowRight size={15} aria-hidden="true" /></Link>
            </div>
          </aside>

          <section className={styles.results} aria-labelledby="paper-results-title" aria-busy={model.isLoading}>
            <div className={styles.resultsHeader}>
              <div className={styles.resultHeading}><h2 id="paper-results-title">{query ? (zh ? "搜索结果" : "Search results") : (zh ? "发现论文" : "Discover papers")}</h2><span aria-live="polite">{model.isLoading ? (zh ? "更新中..." : "Updating...") : `${metrics.paperCount.toLocaleString()} ${zh ? "篇论文" : "papers"}`}</span></div>
              <div className={styles.sortTabs} role="group" aria-label={zh ? "论文排序" : "Paper sort"}>{sorts.map((sort) => <button key={sort.value} type="button" aria-pressed={model.sort === sort.value} onClick={() => model.onSortChange(sort.value)}>{sort[locale]}</button>)}</div>
            </div>
            <div className={styles.paperList}>{papers.map((paper, index) => <PaperRow key={paper.id} paper={paper} locale={locale} onPreview={model.onPreview} renderPdfPreview={index < 3} className={styles.paperRow} />)}</div>
            {!papers.length && <div className={styles.empty}><Search size={32} aria-hidden="true" /><h3>{zh ? "没有找到论文" : "No papers found"}</h3><p>{model.emptyDescription}</p>{hasFilters && <button type="button" onClick={model.onReset}>{zh ? "清除筛选" : "Clear filters"}</button>}</div>}
            <div className={styles.pagination}>{model.pagination}</div>
          </section>
        </div>
      </main>
      <div className={styles.drawer}>{model.drawer}</div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div><dt>{label}</dt><dd>{value.toLocaleString()}</dd></div>
}
