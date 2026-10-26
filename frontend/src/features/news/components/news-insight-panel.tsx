import { FileText, LinkIcon, ShieldCheck } from "lucide-react"
import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { CredibilityBadge } from "@/components/common/credibility-badge"
import { HeatScoreBadge } from "@/components/common/heat-score-badge"
import { QualityBadge } from "@/components/common/quality-badge"
import { ScoreMeter } from "@/components/common/score-meter"
import { SourceBadge } from "@/components/common/source-badge"
import { StatusBadge } from "@/components/common/status-badge"
import { formatDateTime } from "@/lib/format"
import type { EvidenceItem } from "@/types/evidence"
import type { NewsItem } from "@/types/news"
import type { Report } from "@/types/report"
import type { Topic } from "@/types/topic"

export function NewsInsightPanel({
  news,
  evidence,
  topic,
  reports,
}: {
  news: NewsItem
  evidence: EvidenceItem[]
  topic?: Topic
  reports: Report[]
}) {
  return (
    <aside className="space-y-4">
      <PanelCard title="Scores">
        <div className="space-y-4">
          <OptionalScoreMeter label="Heat" value={news.heatScore} />
          <OptionalScoreMeter label="Quality" value={news.qualityScore} />
          <div className="flex flex-wrap gap-2">
            {typeof news.heatScore === "number" ? <HeatScoreBadge value={news.heatScore} /> : <Badge tone="neutral">Heat N/A</Badge>}
            {typeof news.qualityScore === "number" ? <QualityBadge value={news.qualityScore} /> : <Badge tone="neutral">Quality N/A</Badge>}
            <CredibilityBadge value={news.credibility} />
          </div>
        </div>
      </PanelCard>

      <PanelCard title="Source">
        <div className="space-y-3">
          <SourceBadge name={news.sourceName} type={news.sourceType} />
          <p className="text-sm text-[#334155]/68 dark:text-muted-foreground">Published {formatDateTime(news.publishedAt)}</p>
          <p className="text-sm text-[#334155]/68 dark:text-muted-foreground">Collected {formatDateTime(news.collectedAt)}</p>
        </div>
      </PanelCard>

      <PanelCard title="Topic">
        {topic ? (
          <Link href={`/topics/${topic.id}`} className="block rounded-md border border-[#dbe3dc] p-3 hover:bg-[#f7f9f6] dark:border-border dark:hover:bg-secondary">
            <p className="text-sm font-medium text-[#334155] dark:text-foreground">{topic.name}</p>
            <p className="mt-2 line-clamp-3 text-sm text-[#334155]/68 dark:text-muted-foreground">{topic.summary}</p>
          </Link>
        ) : news.topicId && news.topicName ? (
          <div className="rounded-md border border-[#dbe3dc] p-3 dark:border-border">
            <p className="text-sm font-medium text-[#334155] dark:text-foreground">{news.topicName}</p>
            <p className="mt-2 text-sm text-[#334155]/68 dark:text-muted-foreground">Topic relation from the AI News board output.</p>
          </div>
        ) : (
          <p className="text-sm text-[#334155]/60 dark:text-muted-foreground">This news item is not clustered to a topic yet.</p>
        )}
      </PanelCard>

      <PanelCard title="Reports">
        {reports.length ? (
          <div className="space-y-2">
            {reports.map((report) => (
              <Link key={report.id} href={`/reports/${report.id}`} className="flex gap-2 rounded-md border border-[#dbe3dc] p-3 hover:bg-[#f7f9f6] dark:border-border dark:hover:bg-secondary">
                <FileText className="mt-0.5 h-4 w-4 shrink-0 text-emerald-700 dark:text-link" />
                <span className="text-sm text-[#334155] dark:text-foreground">{report.title}</span>
              </Link>
            ))}
          </div>
        ) : (
          <p className="text-sm text-[#334155]/60 dark:text-muted-foreground">No reports include this news item yet.</p>
        )}
      </PanelCard>

      <PanelCard title="Verification">
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-success" />
            <StatusBadge status={evidence.length ? "passed" : "review"} />
          </div>
          <p className="text-sm text-[#334155]/68 dark:text-muted-foreground">
            {evidence.length ? `${evidence.length} evidence item(s) are linked.` : "No evidence is linked yet; keep this item in review."}
          </p>
        </div>
      </PanelCard>

      <PanelCard title="Original source">
        <a href={news.sourceUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 text-sm text-emerald-700 hover:text-[#334155] dark:text-link dark:hover:text-foreground">
          Open source
          <LinkIcon className="h-4 w-4" />
        </a>
      </PanelCard>
    </aside>
  )
}

function PanelCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-md border border-[#dbe3dc] bg-white/85 p-4 dark:border-border dark:bg-card">
      <h2 className="mb-4 text-sm font-semibold text-[#334155] dark:text-foreground">{title}</h2>
      {children}
    </section>
  )
}

function OptionalScoreMeter({ label, value }: { label: string; value?: number }) {
  if (typeof value !== "number") {
    return (
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-3 text-xs text-[#334155]/60 dark:text-muted-foreground">
          <span>{label}</span>
          <span className="font-medium text-[#334155] dark:text-foreground">N/A</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-[#eef3ef] dark:bg-secondary" />
      </div>
    )
  }
  return <ScoreMeter label={label} value={value} />
}
