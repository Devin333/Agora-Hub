import { ExternalLink, FileText, ShieldCheck } from "lucide-react"
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

const rowLinkClass =
  "block px-3 py-2.5 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50"

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
    <aside className="space-y-3" aria-label="News insight">
      <PanelCard title="Scores">
        <div className="space-y-3">
          <OptionalScoreMeter label="Heat" value={news.heatScore} />
          <OptionalScoreMeter label="Quality" value={news.qualityScore} />
          <div className="flex flex-wrap gap-1.5">
            {typeof news.heatScore === "number" ? <HeatScoreBadge value={news.heatScore} /> : <Badge tone="neutral">Heat N/A</Badge>}
            {typeof news.qualityScore === "number" ? <QualityBadge value={news.qualityScore} /> : <Badge tone="neutral">Quality N/A</Badge>}
            <CredibilityBadge value={news.credibility} />
          </div>
        </div>
      </PanelCard>

      <PanelCard title="Source">
        <div className="space-y-2">
          <SourceBadge name={news.sourceName} type={news.sourceType} />
          <p className="text-body2 text-muted-foreground">Published {formatDateTime(news.publishedAt)}</p>
          <p className="text-body2 text-muted-foreground">Collected {formatDateTime(news.collectedAt)}</p>
        </div>
      </PanelCard>

      <PanelCard title="Topic">
        {topic ? (
          <Link href={`/topics/${topic.id}`} className={`overflow-hidden rounded-xl border border-border ${rowLinkClass}`}>
            <p className="text-[13.5px] font-medium text-foreground">{topic.name}</p>
            <p className="mt-1 line-clamp-3 text-[13px] text-muted-foreground">{topic.summary}</p>
          </Link>
        ) : news.topicId && news.topicName ? (
          <div className="rounded-xl border border-border px-3 py-2.5">
            <p className="text-[13.5px] font-medium text-foreground">{news.topicName}</p>
            <p className="mt-1 text-[13px] text-muted-foreground">Topic relation from the AI News board output.</p>
          </div>
        ) : (
          <p className="text-body2 text-muted-foreground">This news item is not clustered to a topic yet.</p>
        )}
      </PanelCard>

      <PanelCard title="Reports">
        {reports.length ? (
          <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
            {reports.map((report) => (
              <Link key={report.id} href={`/reports/${report.id}`} className={`flex gap-2 ${rowLinkClass}`}>
                <FileText size={15} strokeWidth={1.8} className="mt-0.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="text-[13px] text-foreground">{report.title}</span>
              </Link>
            ))}
          </div>
        ) : (
          <p className="text-body2 text-muted-foreground">No reports include this news item yet.</p>
        )}
      </PanelCard>

      <PanelCard title="Verification">
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <ShieldCheck size={16} strokeWidth={1.8} className={evidence.length ? "text-success" : "text-warning"} aria-hidden="true" />
            <StatusBadge status={evidence.length ? "passed" : "review"} />
          </div>
          <p className="text-body2 text-muted-foreground">
            {evidence.length ? `${evidence.length} evidence item(s) are linked.` : "No evidence is linked yet; keep this item in review."}
          </p>
        </div>
      </PanelCard>

      <PanelCard title="Original source">
        <a
          href={news.sourceUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 rounded-sm text-body2 font-medium text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          Open source
          <ExternalLink size={14} strokeWidth={1.8} aria-hidden="true" />
        </a>
      </PanelCard>
    </aside>
  )
}

function PanelCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <h2 className="mb-3 text-body2 font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  )
}

function OptionalScoreMeter({ label, value }: { label: string; value?: number }) {
  if (typeof value !== "number") {
    return (
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-3 text-label text-muted-foreground">
          <span>{label}</span>
          <span className="font-medium text-foreground">N/A</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted" />
      </div>
    )
  }
  return <ScoreMeter label={label} value={value} />
}
