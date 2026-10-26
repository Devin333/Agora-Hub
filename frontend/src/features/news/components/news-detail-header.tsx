import { ExternalLink } from "lucide-react"
import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { CredibilityBadge } from "@/components/common/credibility-badge"
import { HeatScoreBadge } from "@/components/common/heat-score-badge"
import { QualityBadge } from "@/components/common/quality-badge"
import { SourceBadge } from "@/components/common/source-badge"
import { formatDateTime } from "@/lib/format"
import type { NewsItem } from "@/types/news"

export function NewsDetailHeader({ news }: { news: NewsItem }) {
  return (
    <header className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 dark:border-border dark:bg-card">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <SourceBadge name={news.sourceName} type={news.sourceType} />
            <Badge tone="neutral">{news.category}</Badge>
            <CredibilityBadge value={news.credibility} />
          </div>
          <h1 className="mt-4 max-w-5xl text-2xl font-semibold tracking-normal text-[#334155] sm:text-3xl dark:text-foreground">{news.title}</h1>
          <p className="mt-3 max-w-4xl text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">{news.summary}</p>
        </div>
        <a
          href={news.sourceUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex shrink-0 items-center gap-2 rounded-md border border-[#dbe3dc] px-3 py-2 text-sm text-[#334155] hover:bg-[#f7f9f6] dark:border-border dark:text-foreground dark:hover:bg-secondary"
        >
          Open source
          <ExternalLink className="h-4 w-4" />
        </a>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-2">
        {typeof news.heatScore === "number" ? <HeatScoreBadge value={news.heatScore} /> : <Badge tone="neutral">Heat N/A</Badge>}
        {typeof news.qualityScore === "number" ? <QualityBadge value={news.qualityScore} /> : <Badge tone="neutral">Quality N/A</Badge>}
        <Badge tone="neutral">Published {formatDateTime(news.publishedAt)}</Badge>
        <Badge tone="neutral">Collected {formatDateTime(news.collectedAt)}</Badge>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        {news.tags.map((tag) => (
          <span key={tag} className="rounded-md bg-[#eef3ef] px-2 py-1 text-xs text-[#334155]/65 dark:bg-secondary dark:text-muted-foreground">
            {tag}
          </span>
        ))}
      </div>

      {news.topicId && news.topicName ? (
        <div className="mt-4 text-sm text-[#334155]/68 dark:text-muted-foreground">
          Related topic{" "}
          <Link href={`/topics/${news.topicId}`} className="text-emerald-700 hover:text-[#334155] dark:text-link dark:hover:text-foreground">
            {news.topicName}
          </Link>
        </div>
      ) : null}
    </header>
  )
}
