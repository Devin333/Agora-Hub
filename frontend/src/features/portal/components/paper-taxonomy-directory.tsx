"use client"

import { researchPapersHref } from "@/lib/research/routes"
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
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { filterChipClassName, nativeSelectClassName, PaperPortalShell, searchBoxClassName, sidebarRowClassName, sidebarRowMetaClassName, sidebarSectionHeaderClassName } from "./paper-portal-shell"

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

  const sidebar = <>
    <div className="flex items-center gap-1.5 pt-1">
      <Layers3 size={13} strokeWidth={1.8} className="ms-2.5 text-faint-foreground" aria-hidden="true" />
      <h2 className={cn(sidebarSectionHeaderClassName, "ps-0")}>{zh ? "探索方向" : "Explore areas"}</h2>
    </div>
    <p className="px-2.5 pb-2 text-label text-faint-foreground">{zh ? "按方向浏览类别" : "Browse categories by area"}</p>
    <div className="grid gap-px">
      <button type="button" className={sidebarRowClassName} aria-pressed={!group} onClick={() => update({ group: null, category: null })}><span className="min-w-0 flex-1">{zh ? "全部方向" : "All areas"}</span><span className={sidebarRowMetaClassName}>{categories.length}</span></button>
      {previewCategories(directions, group, expanded).map(item => <button type="button" key={item.value} className={sidebarRowClassName} aria-pressed={group === item.value} onClick={() => update({ group: item.value, category: null })}><span className="min-w-0 flex-1 break-words">{directionLabel(item.value)}</span><span className={sidebarRowMetaClassName}>{item.count}</span></button>)}
    </div>
    {directions.length > 5 && <button className="mt-0.5 flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-label text-muted-foreground outline-none transition-colors hover:text-sidebar-foreground focus-visible:ring-3 focus-visible:ring-ring/50" type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? <ChevronUp size={14} strokeWidth={1.8} /> : <ChevronDown size={14} strokeWidth={1.8} />}{expanded ? (zh ? "收起方向" : "Fewer areas") : `${zh ? "展开更多方向" : "More areas"} (${directions.length - 5})`}</button>}
    <div className="mx-2.5 mt-6 border-t border-sidebar-border pt-4 text-label text-muted-foreground">
      <p className="flex items-center gap-2 text-body2 font-medium text-sidebar-foreground"><BookOpen size={15} strokeWidth={1.8} className="text-muted-foreground" />{zh ? "先了解，再深入" : "Explore, then go deeper"}</p>
      <p className="mt-1.5 leading-5">{method ? (zh ? "方法关注怎么做，任务关注解决什么问题。" : "Methods describe how; tasks describe the problem.") : (zh ? "选择一个任务，查看它的研究目标与相关论文。" : "Choose a task to explore its objective and associated papers.")}</p>
      <Link href={researchPapersHref()} className="mt-3 inline-flex items-center gap-1.5 rounded-sm font-medium text-sidebar-foreground underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">{zh ? "返回论文发现" : "Back to papers"}<ArrowRight size={14} strokeWidth={1.8} /></Link>
    </div>
  </>
  const switcherLinkClassName = "inline-flex h-full items-center gap-1.5 rounded-md border border-transparent px-2.5 text-label font-medium text-foreground/60 outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 aria-[current=page]:bg-background aria-[current=page]:text-foreground aria-[current=page]:shadow-sm"
  const linkClassName = "inline-flex items-center gap-1 rounded-sm text-label font-medium text-link underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"

  return <PaperPortalShell locale={locale} sidebarLabel={zh ? "方向筛选" : "Filter by area"} sidebar={sidebar}>
    <header className="flex flex-wrap items-end justify-between gap-4 border-b px-4 py-4 md:px-6">
      <div className="min-w-0">
        <nav className="mb-1.5 flex items-center gap-1.5 text-label text-muted-foreground" aria-label={zh ? "面包屑" : "Breadcrumb"}><Link href={researchPapersHref()} className="rounded-sm outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">{zh ? "论文研究" : "Papers"}</Link><ChevronRight size={13} strokeWidth={1.8} /><span aria-current="page" className="text-foreground">{title}</span></nav>
        <h1 className="text-heading2 font-semibold">{title}</h1>
        <p className="mt-1 text-body2 text-muted-foreground">{method ? (zh ? "理解研究如何展开，找到可借鉴的方法。" : "Understand the approach. Find a method to build on.") : (zh ? "从想解决的问题出发，找到值得研究的方向。" : "Start with a problem. Find a direction to explore.")}</p>
      </div>
      <nav className="inline-flex h-8 shrink-0 items-center rounded-lg bg-muted p-[3px]" aria-label={zh ? "研究目录" : "Research directories"}>
        <Link href="/papers/methods" className={switcherLinkClassName} aria-current={method ? "page" : undefined}><Workflow size={14} strokeWidth={1.8} />{zh ? "研究方法" : "Methods"}</Link>
        <Link href="/papers/tasks" className={switcherLinkClassName} aria-current={!method ? "page" : undefined}><Crosshair size={14} strokeWidth={1.8} />{zh ? "研究任务" : "Tasks"}</Link>
      </nav>
    </header>
    <section className="relative min-w-0 px-4 pb-24 pt-5 md:px-6" aria-label={title} aria-busy={isPending}>
      {isPending && <div aria-hidden="true" className="absolute inset-x-0 top-0 h-0.5 animate-pulse bg-primary/40" />}
      <form role="search" className={searchBoxClassName} onSubmit={search}>
        <Search size={16} strokeWidth={1.8} className="shrink-0" />
        <input className="h-full min-w-0 flex-1 bg-transparent text-body1 text-foreground outline-none placeholder:text-muted-foreground" aria-label={zh ? `搜索${title}` : `Search ${title.toLowerCase()}`} placeholder={method ? (zh ? "搜索方法名称或关键词，如 ReAct、工具使用…" : "Search methods, e.g. ReAct, tool use…") : (zh ? "搜索任务名称或关键词，如智能体、代码生成…" : "Search tasks, e.g. agents, code generation…")} value={draft} onChange={event => setDraft(event.target.value)} />
        {draft && <Button type="button" variant="ghost" size="icon-sm" className="rounded-full text-muted-foreground" aria-label={zh ? "清空搜索" : "Clear search"} onClick={() => { setDraft(""); update({ q: null, category: null }) }}><X size={15} strokeWidth={1.8} /></Button>}
        <Button type="submit" size="sm" className="rounded-lg px-3">{zh ? "搜索" : "Search"}</Button>
      </form>
      <div className="flex min-h-10 items-center gap-2 text-label text-muted-foreground" role="status"><span className="size-1.5 shrink-0 rounded-full bg-faint-foreground" />{source === "cache" ? (zh ? "基于已收录论文缓存" : "Based on the cached paper index") : source === "empty" ? (zh ? "暂无可用论文数据" : "Paper data is not available yet") : (zh ? "基于已收录论文" : "Based on indexed papers")}{allCount === 0 ? (zh ? " · 分类可浏览，论文关联尚待标注" : " · Browse definitions; paper associations are not annotated yet") : (zh ? " · 篇数仅统计已有分类标注" : " · Counts include annotated papers only")}</div>
      {selected ? <>
        <Button type="button" variant="text" size="sm" className="mb-3 mt-1" onClick={() => update({ category: null })}><ArrowLeft size={15} strokeWidth={1.8} />{zh ? "返回分类目录" : "Back to categories"}</Button>
        {active ? <article className="rounded-2xl border bg-card p-5 md:p-6">
          <div className="flex items-center gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-muted text-muted-foreground"><Icon size={18} strokeWidth={1.8} /></span>
            <span className="text-label text-muted-foreground">{directionLabel(active.group || "unclassified")}</span>
          </div>
          <h2 ref={detailHeading} tabIndex={-1} className="mt-4 break-words text-heading2 font-semibold outline-none">{categoryName(active, locale)}</h2>
          {zh && active.nameZh && <p className="mt-0.5 text-body2 text-muted-foreground">{active.name}</p>}
          <p className="mb-6 mt-4 text-body1 leading-7 text-muted-foreground">{(zh ? active.descriptionZh || active.description : active.description) || (zh ? "该类别来自论文分类标注，暂无补充简介。" : "This category comes from paper annotations; no description is available yet.")}</p>
          <div className="flex items-center gap-2 border-t pb-3 pt-4">
            <h3 className="text-body1 font-semibold">{zh ? "关联论文" : "Associated papers"}</h3>
            <span className="text-label text-muted-foreground">{related.length} {zh ? "篇" : "papers"}</span>
            {related.length > 0 && <Link href={`${researchPapersHref()}?${new URLSearchParams({ [kind]: active.value })}`} className={cn(linkClassName, "ms-auto")}>{zh ? "在论文页筛选" : "Filter in papers"}<ArrowRight size={14} strokeWidth={1.8} /></Link>}
          </div>
          {related.length ? <>
            <div className="divide-y rounded-xl border">{related.slice(0, 10).map(paper => <article key={paper.id} className="px-4 py-3">
              <h4><Link href={paperReaderHref(paper.slug || paper.id, returnTo)} onClick={() => rememberPaperScroll(returnTo)} className="group flex items-start justify-between gap-3 rounded-sm text-body1 font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50"><span className="underline-offset-4 group-hover:underline">{paperTitle(paper, locale)}</span><ArrowRight size={15} strokeWidth={1.8} className="mt-0.5 shrink-0 text-muted-foreground" /></Link></h4>
              <p className="mt-1 text-label text-muted-foreground">{paper.authors.slice(0, 3).join(", ")} · {formatPaperDate(paper.publishedAt, locale)}</p>
            </article>)}</div>
            {related.length > 10 && <p className="mt-3 text-label text-muted-foreground">{zh ? "展示最新 10 篇，全部结果可在论文页查看。" : "Showing the latest 10; see all results in papers."}</p>}
          </> : <div className="py-8 text-center text-body2 text-muted-foreground">
            <h4 className="text-body1 font-medium text-foreground">{zh ? "还没有已标注的关联论文" : "No annotated papers yet"}</h4>
            <p className="mt-1">{zh ? "这不代表没有相关研究。你可以先用类别名称检索论文。" : "Related research may still exist. Try searching papers by this category name."}</p>
            <Button asChild variant="outline" size="sm" className="mt-3"><Link href={`${researchPapersHref()}?${new URLSearchParams({ q: active.name })}`}><Search size={14} strokeWidth={1.8} />{zh ? "用名称检索论文" : "Search papers by name"}</Link></Button>
          </div>}
        </article> : <div className="py-16 text-center text-body2 text-muted-foreground">
          <h2 ref={detailHeading} tabIndex={-1} className="text-body1 font-medium text-foreground outline-none">{zh ? "未找到这个类别" : "Category not found"}</h2>
          <p className="mt-1">{zh ? "该类别可能已调整，请返回目录重新选择。" : "The category may have changed. Return to the directory to choose another."}</p>
        </div>}
      </> : <>
        <div className="my-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-baseline gap-3"><h2 className="text-heading3 font-semibold">{group ? directionLabel(group) : (zh ? "全部类别" : "All categories")}</h2><span className="text-body2 text-muted-foreground" aria-live="polite">{filtered.length} {zh ? "个类别" : "categories"}</span></div>
          <label className="flex items-center gap-2 text-label text-muted-foreground">{zh ? "排序" : "Sort"}<select className={nativeSelectClassName} aria-label={zh ? "类别排序" : "Category sorting"} value={sort} onChange={event => update({ sort: event.target.value === "papers" ? null : event.target.value })}><option value="papers">{zh ? "关联论文数" : "Paper count"}</option><option value="name">{zh ? "名称" : "Name"}</option></select></label>
        </div>
        {(query || group) && <div className="mb-3 flex flex-wrap items-center gap-1.5">
          {query && <button type="button" className={filterChipClassName} onClick={() => update({ q: null })}><span>{query}</span><X size={12} strokeWidth={1.8} /></button>}
          {group && <button type="button" className={filterChipClassName} onClick={() => update({ group: null })}><span>{directionLabel(group)}</span><X size={12} strokeWidth={1.8} /></button>}
          <Button type="button" variant="text" size="xs" onClick={() => update({ q: null, group: null, sort: null })}>{zh ? "清除筛选" : "Clear filters"}</Button>
        </div>}
        {filtered.length > 0 && <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{filtered.map(item => <Link key={item.value} href={directoryCategoryHref(pathname, params.toString(), item.value)} className="flex min-h-[200px] flex-col rounded-2xl border bg-card p-4 text-card-foreground outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50" onClick={event => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); update({ category: item.value }, true) } }}>
          <div className="mb-3 flex items-center gap-2.5">
            <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground"><Icon size={15} strokeWidth={1.8} /></span>
            <span className="truncate text-label text-muted-foreground">{directionLabel(item.group || "unclassified")}</span>
            <ChevronRight size={15} strokeWidth={1.8} className="ms-auto shrink-0 text-faint-foreground" />
          </div>
          <h3 className="break-words text-body1 font-semibold">{categoryName(item, locale)}</h3>
          {zh && item.nameZh && <p className="mt-0.5 text-label text-muted-foreground">{item.name}</p>}
          <p className="mb-4 mt-2 line-clamp-3 text-body2 text-muted-foreground">{(zh ? item.descriptionZh || item.description : item.description) || (zh ? "查看类别与相关论文。" : "Explore this category and its papers.")}</p>
          <div className="mt-auto flex items-center justify-between gap-2 border-t pt-3 text-label text-muted-foreground">
            {item.count ? <span className="flex items-center gap-1"><Check size={13} strokeWidth={1.8} />{item.count} {zh ? "篇关联论文" : "associated papers"}</span> : <span className="text-faint-foreground">{zh ? "论文待关联" : "Papers not annotated"}</span>}
            <span className="flex items-center gap-1 whitespace-nowrap font-medium text-foreground">{zh ? "了解类别" : "Explore"}<ArrowRight size={14} strokeWidth={1.8} /></span>
          </div>
        </Link>)}</div>}
        {!filtered.length && <div className="py-16 text-center text-body2 text-muted-foreground">
          <h2 className="text-body1 font-medium text-foreground">{zh ? "没有匹配的类别" : "No matching categories"}</h2>
          <p className="mt-1">{zh ? "试试更短的关键词，或清除方向筛选。" : "Try a shorter keyword or clear the area filter."}</p>
          <Button type="button" variant="link" className="mt-1 text-body2" onClick={() => update({ q: null, group: null })}>{zh ? "查看全部类别" : "Show all categories"}</Button>
        </div>}
      </>}
    </section>
  </PaperPortalShell>
}

function categoryName(category: PaperCategory, locale: Locale) { return locale === "zh" ? category.nameZh || category.name : category.name }
function directoryCategoryHref(pathname: string, search: string, category: string) {
  const params = new URLSearchParams(search)
  params.set("category", category)
  return `${pathname}?${params}`
}
