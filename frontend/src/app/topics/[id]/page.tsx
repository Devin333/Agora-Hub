"use client";

import { use, type ReactNode } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { EmptyState } from "@/components/common/empty-state";
import { buttonVariants } from "@/components/ui/button";
import { TopicDetailTabs } from "@/features/topics/components/topic-detail-tabs";
import { TopicHeader } from "@/features/topics/components/topic-header";
import { TopicTrendChart } from "@/features/topics/components/topic-trend-chart";
import { useTopicDetail } from "@/features/topics/hooks/use-topic-detail";
import { cn } from "@/lib/utils";

export default function TopicDetailPage(props: { params: Promise<{ id: string }> }) {
  const params = use(props.params);
  const { data } = useTopicDetail(params.id);

  if (!data.topic) {
    return (
      <EmptyState
        title="未找到主题"
        description="这个主题 ID 不在情报 mock 数据中。"
        action={
          <Link href="/topics" className={buttonVariants({ variant: "outline", size: "sm" })}>
            <ArrowLeft size={15} strokeWidth={1.8} aria-hidden="true" />
            返回主题
          </Link>
        }
      />
    );
  }

  return (
    <div className="space-y-5">
      <Link href="/topics" className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "-ml-2 text-muted-foreground")}>
        <ArrowLeft size={15} strokeWidth={1.8} aria-hidden="true" />
        返回主题
      </Link>
      <TopicHeader topic={data.topic} />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-5">
          <TopicTrendChart topic={data.topic} />
          <TopicDetailTabs topic={data.topic} evidence={data.evidence} relatedNews={data.relatedNews} relatedTech={data.relatedTech} />
        </div>
        <aside className="space-y-3">
          <InsightCard title="执行摘要">
            <p className="text-[13px] leading-relaxed text-muted-foreground">{data.topic.executiveSummary ?? data.topic.summary}</p>
          </InsightCard>
          <InsightCard title="质量门控">
            {data.topic.qualityGate ? (
              <div className="space-y-2">
                <p className="text-body2 font-medium text-foreground">{data.topic.qualityGate.status}</p>
                <p className="text-[13px] leading-relaxed text-muted-foreground">{data.topic.qualityGate.summary}</p>
                <ul className="list-disc space-y-1 pl-4 text-body2 text-muted-foreground marker:text-faint-foreground">
                  {data.topic.qualityGate.checks.map((check) => (
                    <li key={check}>{check}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-body2 text-muted-foreground">暂无质量门控摘要。</p>
            )}
          </InsightCard>
          <InsightCard title="相关对象">
            <dl className="divide-y divide-border overflow-hidden rounded-xl border border-border text-body2">
              <RelatedCount label="证据" value={data.evidence.length} unit="条" />
              <RelatedCount label="相关新闻" value={data.relatedNews.length} unit="条" />
              <RelatedCount label="相关技术项" value={data.relatedTech.length} unit="个" />
            </dl>
          </InsightCard>
        </aside>
      </div>
    </div>
  );
}

function InsightCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <h2 className="mb-2 text-[15px] font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  );
}

function RelatedCount({ label, value, unit }: { label: string; value: number; unit: string }) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-semibold tabular-nums text-foreground">
        {value} {unit}
      </dd>
    </div>
  );
}
