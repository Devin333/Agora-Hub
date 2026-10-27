"use client"

import { useState } from "react"
import Link from "next/link"
import { buttonVariants } from "@/components/ui/button"
import { PageHeader } from "@/components/layout/page-header"
import { TechFilterToolbar } from "@/features/tech/components/tech-filter-toolbar"
import { TechRadarGrid } from "@/features/tech/components/tech-radar-grid"
import { useTechItems, type TechFilters } from "@/features/tech/hooks/use-tech-items"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { TechItemType } from "@/types/tech"

export function TechPageClient({ fixedType }: { fixedType?: TechItemType }) {
  const { t } = useI18n()
  const [filters, setFilters] = useState<TechFilters>({ type: fixedType })
  const items = useTechItems({ ...filters, type: fixedType ?? filters.type })
  const title = fixedType ? t("portal.tech.typeTitle", { type: techTypeLabel(fixedType, t) }) : t("portal.tech.title")

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow={t("portal.tech.eyebrow")}
        title={title}
        description={t("portal.tech.description")}
        actions={
          <>
            <Link href="/tech/papers" className={buttonVariants({ variant: "outline", size: "sm" })}>{t("portal.tech.paper")}</Link>
            <Link href="/tech/repos" className={buttonVariants({ variant: "outline", size: "sm" })}>{t("portal.tech.repo")}</Link>
            <Link href="/tech/frameworks" className={buttonVariants({ variant: "outline", size: "sm" })}>{t("portal.tech.framework")}</Link>
          </>
        }
      />
      <TechFilterToolbar filters={{ ...filters, type: fixedType ?? filters.type }} onChange={setFilters} />
      {!fixedType ? <RadarOverview /> : null}
      <TechRadarGrid items={items.data} />
    </div>
  )
}

function RadarOverview() {
  const { t } = useI18n()
  const sections = [
    [t("portal.tech.paper"), t("portal.tech.section.paper")],
    [t("portal.tech.repo"), t("portal.tech.section.repo")],
    [t("portal.tech.framework"), t("portal.tech.section.framework")],
    [t("portal.tech.section.emerging"), t("portal.tech.section.emergingSummary")]
  ]
  return (
    <section aria-label={t("portal.tech.title")} className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {sections.map(([title, summary]) => (
        <div key={title} className="rounded-2xl border border-border bg-card p-4">
          <h2 className="text-[15px] font-semibold text-foreground">{title}</h2>
          <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">{summary}</p>
        </div>
      ))}
    </section>
  )
}

function techTypeLabel(type: TechItemType, t: ReturnType<typeof useI18n>["t"]) {
  const keyByType: Record<TechItemType, Parameters<typeof t>[0]> = {
    paper: "portal.tech.paper",
    repo: "portal.tech.repo",
    framework: "portal.tech.framework",
    method: "portal.tech.method",
    practice: "portal.tech.practice"
  }
  return t(keyByType[type])
}
