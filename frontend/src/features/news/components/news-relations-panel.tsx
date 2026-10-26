import Link from "next/link"
import { Badge } from "@/components/common/badge"
import type { NewsItem, RelatedRef } from "@/types/news"

export function NewsRelationsPanel({ news }: { news: NewsItem }) {
  return (
    <section className="rounded-md border border-[#dbe3dc] bg-white/85 p-5 dark:border-border dark:bg-card">
      <h2 className="text-lg font-semibold text-[#334155] dark:text-foreground">Related evidence objects</h2>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <RelationColumn title="Papers" items={news.relatedPapers ?? []} emptyText="No related papers yet." />
        <RelationColumn title="Projects" items={news.relatedProjects ?? []} emptyText="No related projects yet." />
        <RelationColumn title="Community" items={news.relatedCommunityTopics ?? []} emptyText="No community signals yet." />
      </div>
    </section>
  )
}

function RelationColumn({ title, items, emptyText }: { title: string; items: RelatedRef[]; emptyText: string }) {
  return (
    <div className="rounded-md border border-[#edf1ed] bg-[#f7f9f6] p-3 dark:border-border dark:bg-background">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-[#334155] dark:text-foreground">{title}</h3>
        <Badge tone="neutral">{items.length}</Badge>
      </div>
      {items.length ? (
        <div className="mt-3 space-y-2">
          {items.slice(0, 4).map((item) =>
            item.url ? (
              <a key={item.id} href={item.url} target="_blank" rel="noreferrer" className="block text-sm text-emerald-700 hover:text-[#334155] dark:text-link dark:hover:text-foreground">
                {item.title}
              </a>
            ) : (
              <Link key={item.id} href={hrefForRelation(title, item)} className="block text-sm text-emerald-700 hover:text-[#334155] dark:text-link dark:hover:text-foreground">
                {item.title}
              </Link>
            )
          )}
        </div>
      ) : (
        <p className="mt-3 text-sm leading-6 text-[#334155]/60 dark:text-muted-foreground">{emptyText}</p>
      )}
    </div>
  )
}

function hrefForRelation(title: string, item: RelatedRef) {
  if (title === "Papers") return `/papers?paper=${encodeURIComponent(item.id)}`
  if (title === "Projects") return `/projects/${encodeURIComponent(item.id)}`
  return `/community/topics/${encodeURIComponent(item.id)}`
}
