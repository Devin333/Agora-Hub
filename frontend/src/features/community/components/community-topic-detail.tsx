import type { ReactNode } from "react"
import { ArrowLeft, ExternalLink } from "lucide-react"
import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { SectionCaption, SectionPanel } from "@/components/common/section-panel"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import { CommunitySentimentBadge } from "@/features/community/components/community-sentiment-badge"
import { communitySourceLabel } from "@/lib/community/community-filters"
import { formatDateTime } from "@/lib/format"
import type { CommunityDiscussion, CommunityTopicDetail as CommunityTopicDetailType } from "@/types/community"

const textLinkClass =
  "rounded-sm text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"

export function CommunityTopicDetail({ topic }: { topic: CommunityTopicDetailType }) {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Community Pulse"
        title={topic.title}
        description={topic.summary}
        actions={
          <>
            <Button asChild variant="outline" size="sm">
              <Link href="/community">
                <ArrowLeft strokeWidth={1.8} aria-hidden="true" />
                Back
              </Link>
            </Button>
            {topic.sourceUrl ? (
              <Button asChild size="sm">
                <a href={topic.sourceUrl} target="_blank" rel="noreferrer">
                  <ExternalLink strokeWidth={1.8} aria-hidden="true" />
                  Open source
                </a>
              </Button>
            ) : null}
          </>
        }
      />

      <dl className="grid gap-3 sm:grid-cols-2 md:grid-cols-4">
        <MetricCard label="Sentiment" value={<CommunitySentimentBadge sentiment={topic.sentiment} />} />
        <MetricCard label="Heat" value={scoreText(topic.heatScore)} />
        <MetricCard label="Controversy" value={scoreText(topic.controversyScore)} />
        <MetricCard label="Adoption" value={scoreText(topic.adoptionScore)} />
      </dl>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-5">
          <SectionPanel title="Top discussions" framed={false}>
            {topic.topDiscussions.length ? (
              <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
                {topic.topDiscussions.map((discussion) => (
                  <DiscussionItem key={discussion.id} discussion={discussion} />
                ))}
              </div>
            ) : (
              <p className="text-body2 text-muted-foreground">No public discussions are available.</p>
            )}
          </SectionPanel>

          <SectionPanel title="Representative comments">
            {topic.representativeComments.length ? (
              <div className="space-y-3">
                {topic.representativeComments.map((comment) => (
                  <blockquote key={comment.id} className="rounded-xl bg-muted px-3.5 py-3 text-body2 leading-relaxed text-foreground/85">
                    <p>{comment.excerpt}</p>
                    <footer className="mt-1.5 text-label text-muted-foreground">
                      {comment.authorName ?? comment.sourceName ?? "Public excerpt"}
                      {comment.publishedAt ? ` | ${formatDateTime(comment.publishedAt)}` : null}
                    </footer>
                  </blockquote>
                ))}
              </div>
            ) : (
              <p className="text-body2 text-muted-foreground">This artifact does not include public representative comment excerpts.</p>
            )}
          </SectionPanel>

          <SectionPanel title="Evidence references" framed={false}>
            {topic.evidenceRefs?.length ? (
              <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
                {topic.evidenceRefs.map((evidence) => (
                  <div key={evidence.id} className="px-4 py-3">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge tone="accent">{evidence.sourceName ?? evidence.sourceId ?? "Evidence"}</Badge>
                      {evidence.reliability ? <Badge tone="neutral">{evidence.reliability}</Badge> : null}
                    </div>
                    {evidence.excerpt ? <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">{evidence.excerpt}</p> : null}
                    {evidence.url ? (
                      <a className={`mt-1.5 inline-flex text-label ${textLinkClass}`} href={evidence.url} target="_blank" rel="noreferrer">
                        Open evidence
                      </a>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-body2 text-muted-foreground">No public evidence references are available.</p>
            )}
          </SectionPanel>
        </div>

        <aside className="space-y-5" aria-label="Topic context">
          <SectionPanel title="Source distribution">
            <dl className="space-y-2">
              {topic.sourceDistribution.map((item) => (
                <div key={item.sourceType} className="flex items-center justify-between gap-3 text-body2">
                  <dt className="text-muted-foreground">{communitySourceLabel(item.sourceType)}</dt>
                  <dd className="tabular-nums text-foreground">{item.count}</dd>
                </div>
              ))}
            </dl>
          </SectionPanel>

          <SectionPanel title="Related objects" bodyClassName="space-y-3">
            <RelationList title="Papers" items={topic.relatedPapers?.map((paper) => ({ id: paper.id, label: paper.title, url: paper.url })) ?? []} />
            <RelationList title="Projects" items={topic.relatedProjects?.map((project) => ({ id: project.id, label: project.name, url: project.url })) ?? []} />
            <RelationList title="News" items={topic.relatedNews?.map((news) => ({ id: news.id, label: news.title, url: news.url })) ?? []} />
          </SectionPanel>

          <SectionPanel title="Timeline">
            <ol className="space-y-3">
              {topic.timeline.map((item) => (
                <li key={item.id} className="relative pl-4">
                  <span className="absolute left-0 top-1.5 size-1.5 rounded-full bg-faint-foreground" aria-hidden="true" />
                  <p className="text-body2 font-medium text-foreground">{item.label}</p>
                  <p className="mt-0.5 text-label tabular-nums text-muted-foreground">{formatDateTime(item.timestamp)}</p>
                  {item.description ? <p className="mt-0.5 line-clamp-3 text-label text-muted-foreground">{item.description}</p> : null}
                </li>
              ))}
            </ol>
          </SectionPanel>
        </aside>
      </div>
    </div>
  )
}

function MetricCard({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-2xl border border-border bg-card p-4">
      <dt className="text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{label}</dt>
      <dd className="mt-1.5 text-heading3 font-semibold tabular-nums text-foreground">{value}</dd>
    </div>
  )
}

function DiscussionItem({ discussion }: { discussion: CommunityDiscussion }) {
  return (
    <article className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="accent">{communitySourceLabel(discussion.sourceType)}</Badge>
        {discussion.sourceName ? <Badge tone="neutral">{discussion.sourceName}</Badge> : null}
      </div>
      <h3 className="mt-1.5 text-[14px] font-semibold text-foreground">{discussion.title}</h3>
      <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">{discussion.excerpt}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-3 text-label text-muted-foreground">
        {discussion.publishedAt ? <span>{formatDateTime(discussion.publishedAt)}</span> : null}
        {discussion.commentCount !== undefined ? <span>{discussion.commentCount} comments</span> : null}
        {discussion.upvoteCount !== undefined ? <span>{discussion.upvoteCount} score</span> : null}
        {discussion.url ? (
          <a className={textLinkClass} href={discussion.url} target="_blank" rel="noreferrer">
            Open discussion
          </a>
        ) : null}
      </div>
    </article>
  )
}

function RelationList({ title, items }: { title: string; items: Array<{ id: string; label: string; url?: string }> }) {
  return (
    <div>
      <SectionCaption className="mb-1">{title}</SectionCaption>
      {items.length ? (
        <div className="flex flex-wrap gap-1.5">
          {items.map((item) =>
            item.url ? (
              <a
                key={item.id}
                href={item.url}
                target="_blank"
                rel="noreferrer"
                className="rounded-full outline-none hover:opacity-80 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <Badge tone="info">{item.label}</Badge>
              </a>
            ) : (
              <Badge key={item.id} tone="info">
                {item.label}
              </Badge>
            )
          )}
        </div>
      ) : (
        <p className="text-label text-muted-foreground">No public links are available.</p>
      )}
    </div>
  )
}

function scoreText(value?: number) {
  return value === undefined ? "n/a" : Math.round(value)
}
