"use client"

import { useId, useState } from "react"
import Link from "next/link"
import { ArrowUpRight, Check, ChevronDown, ChevronUp, Crosshair, Layers3, Workflow } from "lucide-react"
import { previewCategories, type PaperCategory, type PaperCategoryGroups } from "@/lib/papers/discovery-categories"
import { canonicalRefSlug } from "@/lib/papers/query"
import type { Locale } from "@/lib/papers/types"
import styles from "./papers-design-demo.module.css"

type CategoryKind = "topic" | "method" | "task"

export function PaperCategorySidebar({ groups, locale, selected, onSelect }: {
  groups: PaperCategoryGroups; locale: Locale; selected: Record<CategoryKind, string>; onSelect: (kind: CategoryKind, value: string) => void
}) {
  const zh = locale === "zh"
  return <div className={styles.categoryNavigation}>
    <p className={styles.countScope}>{zh ? "全库关联篇数" : "Papers in the full catalog"}</p>
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
  return <section className={styles.categoryGroup} aria-label={label}>
    <div className={styles.categoryGroupHeading}>
      <h2><button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}><Icon size={16} aria-hidden="true" /><span>{label}</span>{selected && <span className={styles.selectionMark} aria-label={zh ? "已选 1 类" : "1 selected"} />}<ChevronDown size={14} data-open={open} aria-hidden="true" /></button></h2>
      {href && <Link href={href} title={zh ? `查看${label}目录` : `Browse ${label}`} aria-label={zh ? `查看${label}目录` : `Browse ${label}`}><ArrowUpRight size={16} /></Link>}
    </div>
    <div id={id} hidden={!open}>
      <div className={styles.topics}>
        <button type="button" aria-label={zh ? `全部${label.slice(2)}` : `All ${label.toLowerCase()}`} aria-pressed={!selected} onClick={() => onSelect("")}><span>{zh ? "全部" : "All"}</span>{!selected && <Check size={14} />}</button>
        {visible.map(item => <button type="button" key={item.value} disabled={!item.count && selected !== item.value} aria-label={`${name(item)}${item.code ? `, ${item.code}` : ""}, ${item.count ? `${item.count} ${zh ? "篇论文" : "papers"}` : (zh ? "待标注" : "Not annotated")}`} title={item.code || name(item)} aria-pressed={selected === item.value} onClick={() => onSelect(selected === item.value ? "" : item.value)}><span>{name(item)}</span><span>{item.count || (zh ? "待标注" : "Pending")}</span></button>)}
      </div>
      {categories.length > 5 && <button type="button" className={styles.expandCategories} aria-label={`${expanded ? (zh ? "收起" : "Show fewer") : (zh ? "展开更多" : "Show more")} ${label}`} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}{expanded ? (zh ? "收起" : "Show fewer") : `${zh ? "展开更多" : "Show more"} (${categories.length - 5})`}</button>}
      {pending && <p className={styles.categoryPending}>{zh ? "暂无论文关联标注" : "No annotated papers yet"}</p>}
    </div>
  </section>
}
