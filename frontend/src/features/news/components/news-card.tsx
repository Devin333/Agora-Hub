import { ExternalLink } from "lucide-react"
import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { CredibilityBadge } from "@/components/common/credibility-badge"
import { HeatScoreBadge } from "@/components/common/heat-score-badge"
import { QualityBadge } from "@/components/common/quality-badge"
import { SourceBadge } from "@/components/common/source-badge"
import { Button } from "@/components/ui/button"
import { formatDateTime } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { NewsItem } from "@/types/news"

/** News stream row: lives inside the `divide-y rounded-xl border` list from NewsList. */
export function NewsCard({ news, compact = false }: { news: NewsItem; compact?: boolean }) {
  return (
    <article
      className={cn(
        "group grid gap-4 px-4 transition-colors hover:bg-accent/40 md:grid-cols-[minmax(0,1fr)_7.5rem]",
        compact ? "py-3" : "py-4"
      )}
    >
      <div className="min-w-0">
        <div className="flex items-start justify-between gap-3">
          <Link href={`/news/${news.id}`} className="block min-w-0 rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <h3 className={cn("font-semibold leading-snug text-foreground group-hover:underline", compact ? "text-[14px]" : "text-[15px]")}>
              {news.title}
            </h3>
            <p className="mt-1 text-label text-muted-foreground">
              {news.sourceName} · Published {formatDateTime(news.publishedAt)}
              {news.topicName ? ` · ${news.topicName}` : ""}
            </p>
            <p className={cn("mt-2 text-[13px] leading-relaxed text-muted-foreground", compact ? "line-clamp-2" : "line-clamp-3")}>
              {news.summary}
            </p>
          </Link>
          <Button asChild variant="ghost" size="icon-sm" className="shrink-0 text-muted-foreground">
            <a href={news.sourceUrl} target="_blank" rel="noreferrer" aria-label="Open original source">
              <ExternalLink size={15} strokeWidth={1.8} />
            </a>
          </Button>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <Badge tone="accent">{news.category}</Badge>
          <SourceBadge name={news.sourceName} type={news.sourceType} />
          <CredibilityBadge value={news.credibility} />
          <RelationBadge label="papers" value={news.relatedPapers?.length ?? 0} />
          <RelationBadge label="projects" value={news.relatedProjects?.length ?? 0} />
          <RelationBadge label="community" value={news.relatedCommunityTopics?.length ?? 0} />
        </div>

        {news.tags.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {news.tags.slice(0, compact ? 4 : 8).map((tag) => (
              <span key={tag} className="rounded-md bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground">
                {tag}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      <aside className="hidden border-l border-border pl-4 md:block" aria-label="Scores">
        <div className="space-y-3 pt-0.5">
          <Metric value={news.heatScore} label="heat" />
          <Metric value={news.qualityScore} label="quality" />
          <div className="flex flex-col items-start gap-1.5">
            {typeof news.heatScore === "number" ? <HeatScoreBadge value={news.heatScore} /> : <Badge tone="neutral">Heat N/A</Badge>}
            {typeof news.qualityScore === "number" ? <QualityBadge value={news.qualityScore} /> : <Badge tone="neutral">Quality N/A</Badge>}
          </div>
        </div>
      </aside>
    </article>
  )
}

function RelationBadge({ label, value }: { label: string; value: number }) {
  return (
    <span className="inline-flex h-5 items-center rounded-full border border-border px-2 text-[11px] tabular-nums text-muted-foreground">
      {value} {label}
    </span>
  )
}

function Metric({ value, label }: { value?: number; label: string }) {
  return (
    <div>
      <p className="text-heading3 font-semibold tabular-nums text-foreground">{typeof value === "number" ? value : "N/A"}</p>
      <p className="text-caption uppercase tracking-wider text-faint-foreground">{label}</p>
    </div>
  )
}
