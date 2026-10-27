import Link from "next/link"
import { papersCopy, t } from "@/lib/papers/copy"
import { formatWholeNumber, taskName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { MethodAreaDomain } from "@/lib/papers/metrics"
import type { Locale, Paper, TaskRef } from "@/lib/papers/types"
import { cn } from "@/lib/utils"

type SidebarItem = { key: string; href: string; label: string; count: number }

/** Rakazo sidebar: sectioned list of method areas and top tasks with live paper counts. */
export function PapersDomainSidebar({
  methodAreas,
  topTasks,
  dashboardPapers,
  locale,
  className
}: {
  methodAreas: MethodAreaDomain[]
  topTasks: TaskRef[]
  dashboardPapers: Paper[]
  locale: Locale
  className?: string
}) {
  return (
    <aside
      aria-label={locale === "zh" ? "论文领域" : "Paper domains"}
      className={cn(
        "rk-scroll flex flex-col gap-3 rounded-2xl border border-sidebar-border bg-sidebar py-3 text-sidebar-foreground xl:sticky xl:top-24 xl:max-h-[calc(100dvh-7rem)] xl:overflow-y-auto",
        className
      )}
    >
      <SidebarSection
        title={t(papersCopy.methods, locale)}
        items={methodAreas.map((area) => ({
          key: area.slug,
          href: `${papersRoutes.methods}#${area.slug}`,
          label: area.name,
          count: area.count
        }))}
        locale={locale}
      />
      <SidebarSection
        title={t(papersCopy.tasks, locale)}
        items={topTasks.map((task) => ({
          key: task.id,
          href: papersRoutes.taskDetail(task.slug),
          label: taskName(task, locale),
          count: countPapersForTask(dashboardPapers, task.slug)
        }))}
        locale={locale}
      />
    </aside>
  )
}

function SidebarSection({ title, items, locale }: { title: string; items: SidebarItem[]; locale: Locale }) {
  return (
    <section className="px-2.5">
      <h2 className="flex items-center justify-between px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">
        <span>{title}</span>
        <span className="tabular-nums">{formatWholeNumber(items.length, locale)}</span>
      </h2>
      {items.length ? (
        <ul className="mt-1 space-y-0.5">
          {items.map((item) => (
            <li key={item.key}>
              <Link
                href={item.href}
                className="flex items-center gap-3 rounded-xl px-2.5 py-[10px] outline-none transition-colors hover:bg-sidebar-accent/60 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <span className="min-w-0 flex-1 truncate text-body2 font-medium">{item.label}</span>
                <span className="shrink-0 text-label tabular-nums text-faint-foreground">{formatWholeNumber(item.count, locale)}</span>
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-2.5 py-2 text-label text-muted-foreground">{locale === "zh" ? "暂无数据" : "No data yet"}</p>
      )}
    </section>
  )
}

export function countPapersForTask(papers: Paper[], taskSlug: string) {
  return papers.filter(
    (paper) => paper.isPublished !== false && (paper.taskRefs ?? []).some((taskRef) => taskRef.slug === taskSlug)
  ).length
}
