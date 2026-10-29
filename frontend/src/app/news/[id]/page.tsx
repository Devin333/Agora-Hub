"use client";
import { use } from "react";

import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { Button } from "@/components/ui/button"
import { AgentExplanationCard } from "@/features/news/components/agent-explanation-card"
import { AISummaryPanel } from "@/features/news/components/ai-summary-panel"
import { KeyFactsList } from "@/features/news/components/key-facts-list"
import { NewsDetailHeader } from "@/features/news/components/news-detail-header"
import { NewsEvidenceList } from "@/features/news/components/news-evidence-list"
import { NewsInsightPanel } from "@/features/news/components/news-insight-panel"
import { NewsRelationsPanel } from "@/features/news/components/news-relations-panel"
import { useNewsDetail } from "@/features/news/hooks/use-news-detail"

export default function NewsDetailPage(props: { params: Promise<{ id: string }> }) {
  const params = use(props.params);
  const { data, isLoading, isError, error, refetch } = useNewsDetail(params.id)

  if (isLoading) {
    return <PageSkeleton />
  }

  if (isError) {
    return <ErrorState message={error instanceof Error ? error.message : "News detail failed to load."} onRetry={() => refetch()} />
  }

  if (!data?.news) {
    return (
      <EmptyState
        title="News item not found"
        description="This news id is not present in the current AI News data set."
        action={
          <Button asChild variant="outline" size="sm">
            <Link href="/news">Back to news</Link>
          </Button>
        }
      />
    )
  }

  return (
    <div className="space-y-5">
      {data.dataState === "fallback" ? (
        <div role="status" className="flex items-center gap-2 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 text-body2 text-foreground/85">
          <Badge tone="warning">Degraded</Badge>
          <span>This detail view is using the current degraded AI News data state.</span>
        </div>
      ) : null}

      <NewsDetailHeader news={data.news} />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-3">
          <AISummaryPanel summary={data.news.detailedSummary} whyItMatters={data.news.whyItMatters} />
          <KeyFactsList facts={data.news.keyFacts} />
          <NewsEvidenceList evidence={data.evidence} />
          <NewsRelationsPanel news={data.news} />
          <section className="rounded-2xl border border-border bg-card p-4">
            <h2 className="text-[15px] font-semibold text-foreground">Timeline preview</h2>
            <p className="mt-2 text-body2 leading-relaxed text-muted-foreground">
              A full event timeline belongs in the topic and evidence graph surfaces. This detail page keeps the source, evidence, and related object entry points close to the news item.
            </p>
          </section>
          <AgentExplanationCard items={data.news.agentExplanation} />
        </div>
        <NewsInsightPanel news={data.news} evidence={data.evidence} topic={data.topic} reports={data.reports} />
      </div>
    </div>
  )
}
