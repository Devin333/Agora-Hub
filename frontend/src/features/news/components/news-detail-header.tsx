import { ChevronLeft, ExternalLink } from "lucide-react"
import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { CredibilityBadge } from "@/components/common/credibility-badge"
import { HeatScoreBadge } from "@/components/common/heat-score-badge"
import { QualityBadge } from "@/components/common/quality-badge"
import { SourceBadge } from "@/components/common/source-badge"
import { Button } from "@/components/ui/button"
import { formatDateTime } from "@/lib/format"
import type { NewsItem } from "@/types/news"

/** Full-page detail header in the rakazo idiom: back link, heading2 title, hairline bottom. */
export function NewsDetailHeader({ news }: { news: NewsItem }) {
  return (
    <header className="border-b border-border pb-5">
      <Link
        href="/news"
        className="-ml-1 inline-flex items-center gap-0.5 rounded-lg py-1 pe-1.5 text-[13px] font-medium text-muted-foreground outline-none hover:bg-accent hover:text-accent-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <ChevronLeft size={16} strokeWidth={1.9} aria-hidden="true" />
        AI News
      </Link>

      <div className="mt-3 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <SourceBadge name={news.sourceName} type={news.sourceType} />
            <Badge tone="neutral">{news.category}</Badge>
            <CredibilityBadge value={news.credibility} />
          </div>
          <h1 className="mt-3 max-w-5xl text-heading2 font-semibold text-foreground">{news.title}</h1>
          <p className="mt-2 max-w-4xl text-body2 leading-relaxed text-muted-foreground">{news.summary}</p>
        </div>
        <Button asChild variant="outline" size="sm" className="shrink-0">
          <a href={news.sourceUrl} target="_blank" rel="noreferrer">
            Open source
            <ExternalLink strokeWidth={1.8} aria-hidden="true" />
          </a>
        </Button>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-1.5">
        {typeof news.heatScore === "number" ? <HeatScoreBadge value={news.heatScore} /> : <Badge tone="neutral">Heat N/A</Badge>}
        {typeof news.qualityScore === "number" ? <QualityBadge value={news.qualityScore} /> : <Badge tone="neutral">Quality N/A</Badge>}
        <Badge tone="neutral">Published {formatDateTime(news.publishedAt)}</Badge>
        <Badge tone="neutral">Collected {formatDateTime(news.collectedAt)}</Badge>
      </div>

      {news.tags.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {news.tags.map((tag) => (
            <span key={tag} className="rounded-md bg-muted px-2 py-1 text-[11px] font-semibold text-muted-foreground">
              {tag}
            </span>
          ))}
        </div>
      ) : null}

      {news.topicId && news.topicName ? (
        <p className="mt-3 text-body2 text-muted-foreground">
          Related topic{" "}
          <Link href={`/topics/${news.topicId}`} className="font-medium text-link hover:underline">
            {news.topicName}
          </Link>
        </p>
      ) : null}
    </header>
  )
}
