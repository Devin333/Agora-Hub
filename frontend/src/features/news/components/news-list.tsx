import { EmptyState } from "@/components/common/empty-state"
import { NewsCard } from "@/features/news/components/news-card"
import { NewsTable } from "@/features/news/components/news-table"
import type { NewsItem, NewsViewMode } from "@/types/news"

export function NewsList({ items, viewMode }: { items: NewsItem[]; viewMode: NewsViewMode }) {
  if (!items.length) {
    return (
      <EmptyState
        title="No matching news"
        description="Remove one or more filters, broaden the time range, or switch back to newest items."
      />
    )
  }

  if (viewMode === "table") {
    return <NewsTable items={items} />
  }

  return (
    <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
      {items.map((item) => (
        <NewsCard key={item.id} news={item} compact={viewMode === "dense"} />
      ))}
    </div>
  )
}
