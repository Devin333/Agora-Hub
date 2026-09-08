"use client"

import { useEffect, useMemo, useRef, useState, useTransition, type FormEvent } from "react"
import Link from "next/link"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { ArrowLeft, ArrowRight, BookOpen, Check, ChevronDown, ChevronRight, ChevronUp, Crosshair, Layers3, Search, Workflow, X } from "lucide-react"
import { buildPaperCategories, previewCategories, type PaperCategory, type PaperCategoryDefinitions } from "@/lib/papers/discovery-categories"
import { canonicalRefSlug, queryPapers } from "@/lib/papers/query"
import { taskGroupLabel } from "@/lib/papers/categories"
import { paperReaderHref, rememberPaperScroll } from "@/lib/papers/discovery-navigation"
import { formatPaperDate, paperTitle } from "@/lib/papers/format"
import type { Locale, Paper } from "@/lib/papers/types"
import { useUiStore } from "@/stores/ui-store"
import { PaperPortalHeader } from "./paper-portal-header"
import shared from "./papers-design-demo.module.css"
import styles from "./paper-taxonomy-directory.module.css"

type Props = { kind: "method" | "task"; papers: Paper[]; definitions: PaperCategoryDefinitions; source: string }

export function PaperTaxonomyDirectory({ kind, papers, definitions, source }: Props) {
  const locale = useUiStore(state => state.locale)
  const zh = locale === "zh"
  const method = kind === "method"
  const title = method ? (zh ? "研究方法" : "Research methods") : (zh ? "研究任务" : "Research tasks")
  const Icon = method ? Workflow : Crosshair
  const pathname = usePathname()
  const router = useRouter()
  const params = useSearchParams()
  const query = params.get("q") || ""
  const group = params.get("group") || ""
  const selected = canonicalRefSlug(params.get("category") || "", kind)
  const sort = params.get("sort") === "name" ? "name" : "papers"
  const [draft, setDraft] = useState(query)
  const [expanded, setExpanded] = useState(false)
  const [isPending, startTransition] = useTransition()
  const detailHeading = useRef<HTMLHeadingElement>(null)
  const previousSelected = useRef(selected)
  const categories = useMemo(() => {
    const all = buildPaperCategories(papers, definitions)
    return method ? all.methods : all.tasks
  }, [papers, definitions, method])
  const directions = useMemo(() => {
    const counts = new Map<string, number>()
    for (const item of categories) counts.set(item.group || "unclassified", (counts.get(item.group || "unclassified") || 0) + 1)
    return [...counts].map(([value, count]) => ({ value, count })).sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
  }, [categories])
  const active = categories.find(item => item.value === selected)
  const filtered = useMemo(() => categories.filter(item => (!group || (item.group || "unclassified") === group) && [item.name, item.nameZh, item.description, item.descriptionZh, item.value].join(" ").toLocaleLowerCase().includes(query.toLocaleLowerCase().trim())).sort((a, b) => sort === "name" ? categoryName(a, locale).localeCompare(categoryName(b, locale), locale === "zh" ? "zh-CN" : "en") : b.count - a.count), [categories, group, query, sort, locale])
  const related = useMemo(() => active ? queryPapers([...new Map(papers.map(paper => [paper.id, paper])).values()], { [kind]: active.value, sort: "newest" }).papers : [], [papers, active, kind])
  const returnTo = `${pathname}${params.size ? `?${params}` : ""}`
  const allCount = categories.filter(item => item.count > 0).length

  useEffect(() => setDraft(query), [query])
  useEffect(() => {
    if (selected && previousSelected.current !== selected) detailHeading.current?.focus()
    previousSelected.current = selected
  }, [selected])

  function update(values: Record<string, string | null>, push = false) {
    const next = new URLSearchParams(params.toString())
    for (const [key, value] of Object.entries(values)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    const href = `${pathname}${next.size ? `?${next}` : ""}`
    startTransition(() => {
      if (push) router.push(href, { scroll: false })
      else router.replace(href, { scroll: false })
    })
  }
  function search(event: FormEvent) { event.preventDefault(); update({ q: draft.trim(), category: null }) }
  const directionLabel = (value: string) => {
    if (value === "unclassified") return zh ? "其他方向" : "Other areas"
    if (!method) return taskGroupLabel(value, locale)
    const names: Record<string, string> = { "Prompt Engineering": "提示与推理", "Language Models": "语言模型", Agents: "智能体", Transformers: "Transformer 架构" }
    return zh ? names[value] || value : value
  }

  return <div className={`${shared.page} ${styles.directory}`}>
    <PaperPortalHeader locale={locale} />
    <main className={styles.main}>
      <nav className={styles.breadcrumb} aria-label={zh ? "面包屑" : "Breadcrumb"}><Link href="/design-demo/papers">{zh ? "论文研究" : "Papers"}</Link><ChevronRight size={14} /><span aria-current="page">{title}</span></nav>
      <header className={styles.intro}>
        <div><span className={styles.eyebrow}>{method ? "RESEARCH METHODS" : "RESEARCH TASKS"}</span><h1>{title}<span>.</span></h1><p>{method ? (zh ? "理解研究如何展开，找到可借鉴的方法。" : "Understand the approach. Find a method to build on.") : (zh ? "从想解决的问题出发，找到值得研究的方向。" : "Start with a problem. Find a direction to explore.")}</p></div>
        <nav className={styles.switcher} aria-label={zh ? "研究目录" : "Research directories"}><Link href="/papers/methods" aria-current={method ? "page" : undefined}><Workflow size={17} />{zh ? "研究方法" : "Methods"}</Link><Link href="/papers/tasks" aria-current={!method ? "page" : undefined}><Crosshair size={17} />{zh ? "研究任务" : "Tasks"}</Link></nav>
      </header>
      <div className={styles.workspace}>
        <aside className={styles.sidebar} aria-label={zh ? "方向筛选" : "Filter by area"}>
          <div className={styles.sidebarTitle}><Layers3 size={16} /><h2>{zh ? "探索方向" : "Explore areas"}</h2></div>
          <p>{zh ? "按方向浏览类别" : "Browse categories by area"}</p>
          <div className={styles.directions}>
            <button type="button" aria-pressed={!group} onClick={() => update({ group: null, category: null })}><span>{zh ? "全部方向" : "All areas"}</span><span>{categories.length}</span></button>
            {previewCategories(directions, group, expanded).map(item => <button type="button" key={item.value} aria-pressed={group === item.value} onClick={() => update({ group: item.value, category: null })}><span>{directionLabel(item.value)}</span><span>{item.count}</span></button>)}
          </div>
          {directions.length > 5 && <button className={styles.expand} type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}{expanded ? (zh ? "收起方向" : "Fewer areas") : `${zh ? "展开更多方向" : "More areas"} (${directions.length - 5})`}</button>}
          <div className={styles.sidebarNote}><BookOpen size={17} /><strong>{zh ? "先了解，再深入" : "Explore, then go deeper"}</strong><p>{method ? (zh ? "方法关注怎么做，任务关注解决什么问题。" : "Methods describe how; tasks describe the problem.") : (zh ? "选择一个任务，查看它的研究目标与相关论文。" : "Choose a task to explore its objective and associated papers.")}</p><Link href="/design-demo/papers">{zh ? "返回论文发现" : "Back to papers"}<ArrowRight size={14} /></Link></div>
        </aside>
        <section className={styles.content} aria-label={title} aria-busy={isPending}>
          <form role="search" className={styles.search} onSubmit={search}><Search size={19} /><input aria-label={zh ? `搜索${title}` : `Search ${title.toLowerCase()}`} placeholder={method ? (zh ? "搜索方法名称或关键词，如 ReAct、工具使用…" : "Search methods, e.g. ReAct, tool use…") : (zh ? "搜索任务名称或关键词，如智能体、代码生成…" : "Search tasks, e.g. agents, code generation…")} value={draft} onChange={event => setDraft(event.target.value)} />{draft && <button type="button" className={styles.clear} aria-label={zh ? "清空搜索" : "Clear search"} onClick={() => { setDraft(""); update({ q: null, category: null }) }}><X size={17} /></button>}<button type="submit">{zh ? "搜索" : "Search"}</button></form>
          <div className={styles.dataNote} role="status"><span />{source === "cache" ? (zh ? "基于已收录论文缓存" : "Based on the cached paper index") : source === "empty" ? (zh ? "暂无可用论文数据" : "Paper data is not available yet") : (zh ? "基于已收录论文" : "Based on indexed papers")}{allCount === 0 ? (zh ? " · 分类可浏览，论文关联尚待标注" : " · Browse definitions; paper associations are not annotated yet") : (zh ? " · 篇数仅统计已有分类标注" : " · Counts include annotated papers only")}</div>
          {selected ? <>
            <button type="button" className={styles.back} onClick={() => update({ category: null })}><ArrowLeft size={16} />{zh ? "返回分类目录" : "Back to categories"}</button>
            {active ? <article className={styles.detail}>
              <span className={styles.detailIcon}><Icon size={26} /></span><span className={styles.groupLabel}>{directionLabel(active.group || "unclassified")}</span>
              <h2 ref={detailHeading} tabIndex={-1}>{categoryName(active, locale)}</h2>{zh && active.nameZh && <p className={styles.englishName}>{active.name}</p>}
              <p className={styles.description}>{(zh ? active.descriptionZh || active.description : active.description) || (zh ? "该类别来自论文分类标注，暂无补充简介。" : "This category comes from paper annotations; no description is available yet.")}</p>
              <div className={styles.relatedHeading}><h3>{zh ? "关联论文" : "Associated papers"}</h3><span>{related.length} {zh ? "篇" : "papers"}</span>{related.length > 0 && <Link href={`/design-demo/papers?${new URLSearchParams({ [kind]: active.value })}`}>{zh ? "在论文页筛选" : "Filter in papers"}<ArrowRight size={14} /></Link>}</div>
              {related.length ? <div className={styles.relatedPapers}>{related.slice(0, 10).map(paper => <article key={paper.id}><h4><Link href={paperReaderHref(paper.slug || paper.id, returnTo)} onClick={() => rememberPaperScroll(returnTo)}>{paperTitle(paper, locale)}<ArrowRight size={16} /></Link></h4><p>{paper.authors.slice(0, 3).join(", ")} · {formatPaperDate(paper.publishedAt, locale)}</p></article>)}{related.length > 10 && <p>{zh ? "展示最新 10 篇，全部结果可在论文页查看。" : "Showing the latest 10; see all results in papers."}</p>}</div> : <div className={styles.emptyAssociation}><BookOpen size={24} /><h4>{zh ? "还没有已标注的关联论文" : "No annotated papers yet"}</h4><p>{zh ? "这不代表没有相关研究。你可以先用类别名称检索论文。" : "Related research may still exist. Try searching papers by this category name."}</p><Link href={`/design-demo/papers?${new URLSearchParams({ q: active.name })}`}><Search size={15} />{zh ? "用名称检索论文" : "Search papers by name"}</Link></div>}
            </article> : <div className={styles.empty}><Search size={30} /><h2 ref={detailHeading} tabIndex={-1}>{zh ? "未找到这个类别" : "Category not found"}</h2><p>{zh ? "该类别可能已调整，请返回目录重新选择。" : "The category may have changed. Return to the directory to choose another."}</p></div>}
          </> : <>
            <div className={styles.listToolbar}><div><h2>{group ? directionLabel(group) : (zh ? "全部类别" : "All categories")}</h2><span aria-live="polite">{filtered.length} {zh ? "个类别" : "categories"}</span></div><label>{zh ? "排序" : "Sort"}<select aria-label={zh ? "类别排序" : "Category sorting"} value={sort} onChange={event => update({ sort: event.target.value === "papers" ? null : event.target.value })}><option value="papers">{zh ? "关联论文数" : "Paper count"}</option><option value="name">{zh ? "名称" : "Name"}</option></select></label></div>
            {(query || group) && <div className={styles.filters}>{query && <button type="button" onClick={() => update({ q: null })}>{query}<X size={13} /></button>}{group && <button type="button" onClick={() => update({ group: null })}>{directionLabel(group)}<X size={13} /></button>}<button type="button" onClick={() => update({ q: null, group: null, sort: null })}>{zh ? "清除筛选" : "Clear filters"}</button></div>}
            <div className={styles.cards}>{filtered.map(item => <Link key={item.value} href={directoryCategoryHref(pathname, params.toString(), item.value)} className={styles.card} onClick={event => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); update({ category: item.value }, true) } }}><div className={styles.cardTop}><span className={styles.cardIcon}><Icon size={21} /></span><span className={styles.groupLabel}>{directionLabel(item.group || "unclassified")}</span><ChevronRight size={17} /></div><h3>{categoryName(item, locale)}</h3>{zh && item.nameZh && <p className={styles.englishName}>{item.name}</p>}<p className={styles.cardDescription}>{(zh ? item.descriptionZh || item.description : item.description) || (zh ? "查看类别与相关论文。" : "Explore this category and its papers.")}</p><div className={styles.cardFooter}>{item.count ? <span><Check size={13} />{item.count} {zh ? "篇关联论文" : "associated papers"}</span> : <span>{zh ? "论文待关联" : "Papers not annotated"}</span>}<span>{zh ? "了解类别" : "Explore"}<ArrowRight size={14} /></span></div></Link>)}</div>
            {!filtered.length && <div className={styles.empty}><Search size={30} /><h2>{zh ? "没有匹配的类别" : "No matching categories"}</h2><p>{zh ? "试试更短的关键词，或清除方向筛选。" : "Try a shorter keyword or clear the area filter."}</p><button type="button" onClick={() => update({ q: null, group: null })}>{zh ? "查看全部类别" : "Show all categories"}</button></div>}
          </>}
        </section>
      </div>
    </main>
  </div>
}

function categoryName(category: PaperCategory, locale: Locale) { return locale === "zh" ? category.nameZh || category.name : category.name }
function directoryCategoryHref(pathname: string, search: string, category: string) {
  const params = new URLSearchParams(search)
  params.set("category", category)
  return `${pathname}?${params}`
}
