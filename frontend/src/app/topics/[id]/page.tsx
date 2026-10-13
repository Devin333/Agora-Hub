"use client";;
import { use } from "react";

import Link from "next/link";
import { EmptyState } from "@/components/common/empty-state";
import { TopicDetailTabs } from "@/features/topics/components/topic-detail-tabs";
import { TopicHeader } from "@/features/topics/components/topic-header";
import { TopicTrendChart } from "@/features/topics/components/topic-trend-chart";
import { useTopicDetail } from "@/features/topics/hooks/use-topic-detail";

export default function TopicDetailPage(props: { params: Promise<{ id: string }> }) {
  const params = use(props.params);
  const { data } = useTopicDetail(params.id);

  if (!data.topic) {
    return (
      <EmptyState
        title="未找到主题"
        description="这个主题 ID 不在情报 mock 数据中。"
        action={
          <Link href="/topics" className="rounded-md border border-border px-3 py-2 text-sm text-foreground hover:bg-secondary">
            返回主题
          </Link>
        }
      />
    );
  }

  return (
    <main className="space-y-6">
      <TopicHeader topic={data.topic} />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-6">
          <TopicTrendChart topic={data.topic} />
          <TopicDetailTabs topic={data.topic} evidence={data.evidence} relatedNews={data.relatedNews} relatedTech={data.relatedTech} />
        </div>
        <aside className="space-y-4">
          <InsightCard title="执行摘要">
            <p className="text-sm leading-6 text-muted-foreground">{data.topic.executiveSummary ?? data.topic.summary}</p>
          </InsightCard>
          <InsightCard title="质量门控">
            {data.topic.qualityGate ? (
              <div className="space-y-3">
                <p className="text-sm font-medium text-foreground">{data.topic.qualityGate.status}</p>
                <p className="text-sm leading-6 text-muted-foreground">{data.topic.qualityGate.summary}</p>
                <ul className="space-y-2 text-sm text-muted-foreground">
                  {data.topic.qualityGate.checks.map((check) => (
                    <li key={check}>{check}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">暂无质量门控摘要。</p>
            )}
          </InsightCard>
          <InsightCard title="相关对象">
            <div className="grid gap-2 text-sm text-muted-foreground">
              <span>{data.evidence.length} 条证据</span>
              <span>{data.relatedNews.length} 条相关新闻</span>
              <span>{data.relatedTech.length} 个相关技术项</span>
            </div>
          </InsightCard>
        </aside>
      </div>
    </main>
  );
}

function InsightCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <h2 className="mb-3 text-sm font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  );
}
