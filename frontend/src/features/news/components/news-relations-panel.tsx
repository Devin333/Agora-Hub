import Link from "next/link"
import { Badge } from "@/components/common/badge"
import type { NewsItem, RelatedRef } from "@/types/news"

export function NewsRelationsPanel({ news }: { news: NewsItem }) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <h2 className="text-[15px] font-semibold text-foreground">Related evidence objects</h2>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <RelationColumn title="Papers" items={news.relatedPapers ?? []} emptyText="No related papers yet." />
        <RelationColumn title="Projects" items={news.relatedProjects ?? []} emptyText="No related projects yet." />
        <RelationColumn title="Community" items={news.relatedCommunityTopics ?? []} emptyText="No community signals yet." />
      </div>
    </section>
  )
}

const relationLinkClass =
  "block truncate rounded-sm py-1.5 text-[13px] text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"

function RelationColumn({ title, items, emptyText }: { title: string; items: RelatedRef[]; emptyText: string }) {
  return (
    <div className="rounded-xl border border-border px-3 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-body2 font-medium text-foreground">{title}</h3>
        <Badge tone="neutral">{items.length}</Badge>
      </div>
      {items.length ? (
        <div className="mt-1.5">
          {items.slice(0, 4).map((item) =>
            item.url ? (
              <a key={item.id} href={item.url} target="_blank" rel="noreferrer" className={relationLinkClass}>
                {item.title}
              </a>
            ) : (
              <Link key={item.id} href={hrefForRelation(title, item)} className={relationLinkClass}>
                {item.title}
              </Link>
            )
          )}
        </div>
      ) : (
        <p className="mt-1.5 text-[13px] text-muted-foreground">{emptyText}</p>
      )}
    </div>
  )
}

function hrefForRelation(title: string, item: RelatedRef) {
  if (title === "Papers") return `/papers?paper=${encodeURIComponent(item.id)}`
  if (title === "Projects") return `/projects/${encodeURIComponent(item.id)}`
  return `/community/topics/${encodeURIComponent(item.id)}`
}
