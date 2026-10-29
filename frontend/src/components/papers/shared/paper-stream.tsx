"use client"

import { useMemo, useState } from "react"
import { PaperRow } from "@/components/papers/paper-row"
import { PaperSortTabs } from "@/components/papers/shared/paper-sort-tabs"
import { Button } from "@/components/ui/button"
import { papersCopy, t } from "@/lib/papers/copy"
import { sortPapers } from "@/lib/papers/format"
import type { Locale, Paper, PaperSort } from "@/lib/papers/types"

const PAGE_SIZE = 100

export function PaperStream({
  papers,
  locale,
  title,
  emptyDescription,
  sort: controlledSort,
  onSortChange,
  onPreview
}: {
  papers: Paper[]
  locale: Locale
  title: string
  emptyDescription?: string
  sort?: PaperSort
  onSortChange?: (sort: PaperSort) => void
  onPreview: (paper: Paper) => void
}) {
  const [localSort, setLocalSort] = useState<PaperSort>("trending")
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE)
  const sort = controlledSort ?? localSort
  const sortedPapers = useMemo(() => (controlledSort ? papers : sortPapers(papers, sort)), [controlledSort, papers, sort])
  const visiblePapers = sortedPapers.slice(0, visibleCount)
  const hasMore = visiblePapers.length < sortedPapers.length

  function handleSortChange(nextSort: PaperSort) {
    setVisibleCount(PAGE_SIZE)
    if (controlledSort !== undefined) {
      onSortChange?.(nextSort)
      return
    }
    setLocalSort(nextSort)
  }

  return (
    <section className="overflow-hidden rounded-xl border">
      <div className="flex flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        {title ? <h2 className="text-body1 font-semibold text-foreground">{title}</h2> : <div className="hidden sm:block" />}
        <PaperSortTabs value={sort} locale={locale} onChange={handleSortChange} />
      </div>
      {sortedPapers.length ? (
        <>
          <div className="divide-y">
            {visiblePapers.map((paper, index) => (
              <PaperRow key={paper.id} paper={paper} locale={locale} onPreview={onPreview} renderPdfPreview={index < 3} />
            ))}
          </div>
          {hasMore ? (
            <div className="flex justify-center border-t px-4 py-3">
              <Button
                type="button"
                variant="outline"
                className="px-4"
                onClick={() => setVisibleCount((count) => Math.min(count + PAGE_SIZE, sortedPapers.length))}
              >
                {t(papersCopy.loadMorePapers, locale)}
                <span className="tabular-nums text-muted-foreground">
                  {visiblePapers.length}/{sortedPapers.length}
                </span>
              </Button>
            </div>
          ) : null}
        </>
      ) : (
        <div className="px-4 py-10 text-center text-body2 text-muted-foreground">
          <p>{t(papersCopy.noPapers, locale)}</p>
          {emptyDescription ? (
            <p className="mx-auto mt-2 max-w-2xl leading-6">{emptyDescription}</p>
          ) : null}
        </div>
      )}
    </section>
  )
}
