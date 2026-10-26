import type { ReactNode } from "react"
import { ExternalLink } from "lucide-react"
import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { CommunitySentimentBadge } from "@/features/community/components/community-sentiment-badge"
import { communitySourceLabel } from "@/lib/community/community-filters"
import { formatDateTime } from "@/lib/format"
import type { CommunityDiscussion, CommunityTopicDetail as CommunityTopicDetailType } from "@/types/community"

export function CommunityTopicDetail({ topic }: { topic: CommunityTopicDetailType }) {
  return (
    <main className="space-y-6">
      <PageHeader
        eyebrow="Community Pulse"
        title={topic.title}
        description={topic.summary}
        actions={
          <>
            <Button asChild variant="outline" size="sm">
              <Link href="/community">Back</Link>
            </Button>
            {topic.sourceUrl ? (
              <Button asChild size="sm">
                <a href={topic.sourceUrl} target="_blank" rel="noreferrer">
                  <ExternalLink className="size-4" />
                  Open source
                </a>
              </Button>
            ) : null}
          </>
        }
      />

      <section className="grid gap-3 md:grid-cols-4">
        <MetricCard label="Sentiment" value={<CommunitySentimentBadge sentiment={topic.sentiment} />} />
        <MetricCard label="Heat" value={scoreText(topic.heatScore)} />
        <MetricCard label="Controversy" value={scoreText(topic.controversyScore)} />
        <MetricCard label="Adoption" value={scoreText(topic.adoptionScore)} />
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-6">
          <DetailPanel title="Top discussions">
            <div className="space-y-4">
              {topic.topDiscussions.map((discussion) => (
                <DiscussionItem key={discussion.id} discussion={discussion} />
              ))}
            </div>
          </DetailPanel>

          <DetailPanel title="Representative comments">
            {topic.representativeComments.length ? (
              <div className="space-y-3">
                {topic.representativeComments.map((comment) => (
                  <blockquote key={comment.id} className="border-l-2 border-border pl-4 text-sm leading-6 text-muted-foreground">
                    <p>{comment.excerpt}</p>
                    <footer className="mt-2 text-xs text-muted-foreground">
                      {comment.authorName ?? comment.sourceName ?? "Public excerpt"}
                      {comment.publishedAt ? ` | ${formatDateTime(comment.publishedAt)}` : null}
                    </footer>
                  </blockquote>
                ))}
              </div>
            ) : (
              <p className="text-sm leading-6 text-muted-foreground">
                This artifact does not include public representative comment excerpts.
              </p>
            )}
          </DetailPanel>

          <DetailPanel title="Evidence references">
            {topic.evidenceRefs?.length ? (
              <div className="space-y-3">
                {topic.evidenceRefs.map((evidence) => (
                  <div key={evidence.id} className="rounded-md border border-border p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone="accent">{evidence.sourceName ?? evidence.sourceId ?? "Evidence"}</Badge>
                      {evidence.reliability ? <Badge tone="neutral">{evidence.reliability}</Badge> : null}
                    </div>
                    {evidence.excerpt ? <p className="mt-2 text-sm leading-6 text-muted-foreground">{evidence.excerpt}</p> : null}
                    {evidence.url ? (
                      <a className="mt-2 inline-flex text-xs text-link hover:text-foreground" href={evidence.url} target="_blank" rel="noreferrer">
                        Open evidence
                      </a>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No public evidence references are available.</p>
            )}
          </DetailPanel>
        </div>

        <aside className="space-y-6">
          <DetailPanel title="Source distribution">
            <div className="space-y-3">
              {topic.sourceDistribution.map((item) => (
                <div key={item.sourceType} className="flex items-center justify-between gap-3 text-sm">
                  <span className="text-muted-foreground">{communitySourceLabel(item.sourceType)}</span>
                  <span className="font-mono text-foreground">{item.count}</span>
                </div>
              ))}
            </div>
          </DetailPanel>

          <DetailPanel title="Related objects">
            <RelationList title="Papers" items={topic.relatedPapers?.map((paper) => ({ id: paper.id, label: paper.title, url: paper.url })) ?? []} />
            <RelationList title="Projects" items={topic.relatedProjects?.map((project) => ({ id: project.id, label: project.name, url: project.url })) ?? []} />
            <RelationList title="News" items={topic.relatedNews?.map((news) => ({ id: news.id, label: news.title, url: news.url })) ?? []} />
          </DetailPanel>

          <DetailPanel title="Timeline">
            <div className="space-y-4">
              {topic.timeline.map((item) => (
                <div key={item.id} className="border-l-2 border-border pl-3">
                  <p className="text-sm font-medium text-foreground">{item.label}</p>
                  <p className="mt-1 font-mono text-xs text-muted-foreground">{formatDateTime(item.timestamp)}</p>
                  {item.description ? <p className="mt-1 line-clamp-3 text-xs leading-5 text-muted-foreground">{item.description}</p> : null}
                </div>
              ))}
            </div>
          </DetailPanel>
        </aside>
      </div>
    </main>
  )
}

function MetricCard({ label, value }: { label: string; value: ReactNode }) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-xs font-medium uppercase tracking-normal text-muted-foreground">{label}</p>
        <div className="mt-2 text-lg font-semibold text-foreground">{value}</div>
      </CardContent>
    </Card>
  )
}

function DetailPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function DiscussionItem({ discussion }: { discussion: CommunityDiscussion }) {
  return (
    <article className="border-b border-border pb-4 last:border-b-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">{communitySourceLabel(discussion.sourceType)}</Badge>
        {discussion.sourceName ? <Badge tone="neutral">{discussion.sourceName}</Badge> : null}
      </div>
      <h3 className="mt-2 text-base font-semibold text-foreground">{discussion.title}</h3>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">{discussion.excerpt}</p>
      <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        {discussion.publishedAt ? <span>{formatDateTime(discussion.publishedAt)}</span> : null}
        {discussion.commentCount !== undefined ? <span>{discussion.commentCount} comments</span> : null}
        {discussion.upvoteCount !== undefined ? <span>{discussion.upvoteCount} score</span> : null}
        {discussion.url ? (
          <a className="text-link hover:text-foreground" href={discussion.url} target="_blank" rel="noreferrer">
            Open discussion
          </a>
        ) : null}
      </div>
    </article>
  )
}

function RelationList({ title, items }: { title: string; items: Array<{ id: string; label: string; url?: string }> }) {
  return (
    <div className="mb-4 last:mb-0">
      <p className="mb-2 text-xs font-medium uppercase tracking-normal text-muted-foreground">{title}</p>
      {items.length ? (
        <div className="flex flex-wrap gap-2">
          {items.map((item) =>
            item.url ? (
              <a key={item.id} href={item.url} target="_blank" rel="noreferrer">
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
        <p className="text-sm text-muted-foreground">No public links are available.</p>
      )}
    </div>
  )
}

function scoreText(value?: number) {
  return value === undefined ? "n/a" : Math.round(value)
}
