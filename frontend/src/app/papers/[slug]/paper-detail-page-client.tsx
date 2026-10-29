"use client"

import { PaperDetailContent } from "@/components/papers/shared/paper-detail-content"
import { PapersMicrobar } from "@/components/papers/papers-microbar"
import { paperTitle } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Paper } from "@/lib/papers/types"
import { useUiStore } from "@/stores/ui-store"

export function PaperDetailPageClient({ paper }: { paper: Paper }) {
  const locale = useUiStore((state) => state.locale)
  const title = paperTitle(paper, locale)

  return (
    <div className="space-y-8">
      <PapersMicrobar
        items={[{ label: "Papers", href: papersRoutes.trending }, { label: title }]}
        meta={locale === "zh" ? "论文详情" : "Paper detail"}
        locale={locale}
      />
      <section className="border-b border-border px-4 pb-10 pt-1 md:px-6">
        <PaperDetailContent paper={paper} locale={locale} titleLevel={1} />
      </section>
    </div>
  )
}
