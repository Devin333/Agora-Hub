"use client"

import { useId, useState } from "react"
import Link from "next/link"
import { ArrowUpRight, Check, ChevronDown, ChevronUp, Crosshair, Layers3, Workflow } from "lucide-react"
import { previewCategories, type PaperCategory, type PaperCategoryGroups } from "@/lib/papers/discovery-categories"
import { canonicalRefSlug } from "@/lib/papers/query"
import type { Locale } from "@/lib/papers/types"
import { cn } from "@/lib/utils"
import { sidebarRowClassName, sidebarRowMetaClassName, sidebarSectionHeaderClassName } from "./paper-portal-shell"

type CategoryKind = "topic" | "method" | "task"

export function PaperCategorySidebar({ groups, locale, selected, onSelect }: {
  groups: PaperCategoryGroups; locale: Locale; selected: Record<CategoryKind, string>; onSelect: (kind: CategoryKind, value: string) => void
}) {
  const zh = locale === "zh"
  return <div className="mt-4 space-y-3">
    <p className="px-2.5 text-label text-faint-foreground">{zh ? "全库关联篇数" : "Papers in the full catalog"}</p>
    {([
      { kind: "topic", items: groups.topics, label: zh ? "研究领域" : "Research topics", icon: Layers3 },
      { kind: "method", items: groups.methods, label: zh ? "研究方法" : "Research methods", icon: Workflow, href: "/papers/methods" },
      { kind: "task", items: groups.tasks, label: zh ? "研究任务" : "Research tasks", icon: Crosshair, href: "/papers/tasks" }
    ] as const).map(group => <CategoryGroup key={group.kind} {...group} href={"href" in group ? group.href : undefined} locale={locale} selected={group.kind === "topic" ? selected.topic : canonicalRefSlug(selected[group.kind], group.kind)} onSelect={value => onSelect(group.kind, value)} />)}
  </div>
}

function CategoryGroup({ items, label, icon: Icon, href, selected, onSelect, locale }: {
  items: PaperCategory[]; label: string; icon: typeof Layers3; href?: string; selected: string; onSelect: (value: string) => void; locale: Locale
}) {
  const id = useId()
  const [open, setOpen] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const zh = locale === "zh"
  const name = (item: PaperCategory) => zh ? item.nameZh || item.name : item.name
  const categories = selected && !items.some(item => item.value === selected) ? [...items, { value: selected, name: selected, count: 0 }] : items
  const visible = previewCategories(categories, selected, expanded)
  const pending = categories.length > 0 && categories.every(item => !item.count)
  return <section aria-label={label}>
    <div className="flex items-center gap-1 pe-1">
      <h2 className="min-w-0 flex-1">
        <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)} className={cn(sidebarSectionHeaderClassName, "flex w-full items-center gap-1.5 rounded-lg text-left outline-none transition-colors hover:text-muted-foreground focus-visible:ring-3 focus-visible:ring-ring/50")}>
          <Icon size={13} strokeWidth={1.8} aria-hidden="true" />
          <span className="truncate">{label}</span>
          {selected && <span className="size-1.5 shrink-0 rounded-full bg-primary" aria-label={zh ? "已选 1 类" : "1 selected"} />}
          <ChevronDown size={13} strokeWidth={1.8} aria-hidden="true" className={cn("ms-auto shrink-0 transition-transform", !open && "-rotate-90")} />
        </button>
      </h2>
      {href && <Link href={href} title={zh ? `查看${label}目录` : `Browse ${label}`} aria-label={zh ? `查看${label}目录` : `Browse ${label}`} className="grid size-7 shrink-0 place-items-center rounded-full text-faint-foreground outline-none transition-colors hover:bg-sidebar-accent/60 hover:text-sidebar-foreground focus-visible:ring-3 focus-visible:ring-ring/50"><ArrowUpRight size={15} strokeWidth={1.8} /></Link>}
    </div>
    <div id={id} hidden={!open} className="mt-0.5">
      <div className="grid gap-px">
        <button type="button" className={sidebarRowClassName} aria-label={zh ? `全部${label.slice(2)}` : `All ${label.toLowerCase()}`} aria-pressed={!selected} onClick={() => onSelect("")}><span className="min-w-0 flex-1">{zh ? "全部" : "All"}</span>{!selected && <Check size={15} strokeWidth={1.8} className="shrink-0 text-muted-foreground" />}</button>
        {visible.map(item => <button type="button" key={item.value} className={sidebarRowClassName} disabled={!item.count && selected !== item.value} aria-label={`${name(item)}${item.code ? `, ${item.code}` : ""}, ${item.count ? `${item.count} ${zh ? "篇论文" : "papers"}` : (zh ? "待标注" : "Not annotated")}`} title={item.code || name(item)} aria-pressed={selected === item.value} onClick={() => onSelect(selected === item.value ? "" : item.value)}><span className="min-w-0 flex-1 break-words">{name(item)}</span><span className={sidebarRowMetaClassName}>{item.count || (zh ? "待标注" : "Pending")}</span></button>)}
      </div>
      {categories.length > 5 && <button type="button" className="mt-0.5 flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-label text-muted-foreground outline-none transition-colors hover:text-sidebar-foreground focus-visible:ring-3 focus-visible:ring-ring/50" aria-label={`${expanded ? (zh ? "收起" : "Show fewer") : (zh ? "展开更多" : "Show more")} ${label}`} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? <ChevronUp size={14} strokeWidth={1.8} /> : <ChevronDown size={14} strokeWidth={1.8} />}{expanded ? (zh ? "收起" : "Show fewer") : `${zh ? "展开更多" : "Show more"} (${categories.length - 5})`}</button>}
      {pending && <p className="px-2.5 py-1 text-label text-faint-foreground">{zh ? "暂无论文关联标注" : "No annotated papers yet"}</p>}
    </div>
  </section>
}
