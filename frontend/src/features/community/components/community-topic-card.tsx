import type { ReactNode } from "react"
import { ExternalLink, Flame, GitBranch, MessageSquare, RadioTower } from "lucide-react"
import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { Button } from "@/components/ui/button"
import { CommunitySentimentBadge } from "@/features/community/components/community-sentiment-badge"
import { communitySourceLabel } from "@/lib/community/community-filters"
import { formatDateTime } from "@/lib/format"
import type { CommunityTopic } from "@/types/community"

/** Rakazo card for one community topic: title block, tag row, relation pills and a score aside. */
export function CommunityTopicCard({ topic }: { topic: CommunityTopic }) {
  return (
    <article className="group rounded-2xl border border-border bg-card p-4 transition-colors hover:bg-accent/40">
      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_7.5rem]">
        <div className="min-w-0">
          <div className="flex items-start justify-between gap-3">
            <Link
              href={`/community/topics/${topic.slug}`}
              className="min-w-0 rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              <h3 className="text-[15px] font-semibold leading-snug text-foreground group-hover:underline">{topic.title}</h3>
              <p className="mt-1 text-label text-muted-foreground">
                {topic.sourceName ?? communitySourceLabel(topic.sourceType)}
                {topic.lastActivityAt ? ` | ${formatDateTime(topic.lastActivityAt)}` : null}
              </p>
            </Link>
            <CommunitySentimentBadge sentiment={topic.sentiment} />
          </div>

          <p className="mt-2 line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">{topic.summary}</p>

          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            <Badge tone="accent">{communitySourceLabel(topic.sourceType)}</Badge>
            {topic.tags.slice(0, 5).map((tag) => (
              <Badge key={tag} tone="neutral">
                {tag}
              </Badge>
            ))}
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5">
            {topic.relatedPapers?.slice(0, 2).map((paper) => (
              <RelationBadge key={paper.id} label={paper.title} url={paper.url} />
            ))}
            {topic.relatedProjects?.slice(0, 2).map((project) => (
              <RelationBadge key={project.id} label={project.name} url={project.url} />
            ))}
            {topic.relatedNews?.slice(0, 2).map((news) => (
              <RelationBadge key={news.id} label={news.title} url={news.url} />
            ))}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button asChild variant="outline" size="sm">
              <Link href={`/community/topics/${topic.slug}`}>View topic</Link>
            </Button>
            {topic.sourceUrl ? (
              <Button asChild variant="ghost" size="sm">
                <a href={topic.sourceUrl} target="_blank" rel="noreferrer">
                  <ExternalLink strokeWidth={1.8} aria-hidden="true" />
                  Open source
                </a>
              </Button>
            ) : null}
          </div>
        </div>

        <aside
          aria-label="Topic scores"
          className="grid grid-cols-2 gap-3 border-t border-border pt-3 md:block md:space-y-3 md:border-l md:border-t-0 md:pl-4 md:pt-0"
        >
          <Metric icon={<Flame size={15} strokeWidth={1.8} />} label="Heat" value={topic.heatScore} />
          <Metric icon={<GitBranch size={15} strokeWidth={1.8} />} label="Controversy" value={topic.controversyScore} />
          <Metric icon={<RadioTower size={15} strokeWidth={1.8} />} label="Adoption" value={topic.adoptionScore} />
          {topic.commentCount !== undefined ? (
            <Metric icon={<MessageSquare size={15} strokeWidth={1.8} />} label="Comments" value={topic.commentCount} />
          ) : null}
        </aside>
      </div>
    </article>
  )
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value?: number }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center justify-center gap-1.5 text-foreground md:justify-start">
        <span className="text-muted-foreground" aria-hidden="true">
          {icon}
        </span>
        <span className="text-heading3 font-semibold tabular-nums">{value === undefined ? "n/a" : Math.round(value)}</span>
      </div>
      <p className="mt-0.5 text-center text-[11px] uppercase tracking-wider text-faint-foreground md:text-left">{label}</p>
    </div>
  )
}

function RelationBadge({ label, url }: { label: string; url?: string }) {
  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="rounded-full outline-none hover:opacity-80 focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <Badge tone="info">{label}</Badge>
      </a>
    )
  }
  return <Badge tone="info">{label}</Badge>
}
