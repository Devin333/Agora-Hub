"use client";
import { use } from "react";

import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
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
          <Link href="/news" className="rounded-md border border-border px-3 py-2 text-sm text-foreground hover:bg-secondary">
            Back to news
          </Link>
        }
      />
    )
  }

  return (
    <main className="space-y-6 font-papers-research">
      {data.dataState === "fallback" ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
          <Badge tone="warning">Degraded</Badge>
          <span className="ml-2">This detail view is using the current degraded AI News data state.</span>
        </div>
      ) : null}

      <NewsDetailHeader news={data.news} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-5">
          <AISummaryPanel summary={data.news.detailedSummary} whyItMatters={data.news.whyItMatters} />
          <KeyFactsList facts={data.news.keyFacts} />
          <NewsEvidenceList evidence={data.evidence} />
          <NewsRelationsPanel news={data.news} />
          <section className="rounded-md border border-[#dbe3dc] bg-white/85 p-5 dark:border-border dark:bg-card">
            <h2 className="text-lg font-semibold text-[#334155] dark:text-foreground">Timeline preview</h2>
            <p className="mt-3 text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">
              A full event timeline belongs in the topic and evidence graph surfaces. This detail page keeps the source, evidence, and related object entry points close to the news item.
            </p>
          </section>
          <AgentExplanationCard items={data.news.agentExplanation} />
        </div>
        <NewsInsightPanel news={data.news} evidence={data.evidence} topic={data.topic} reports={data.reports} />
      </div>
    </main>
  )
}
