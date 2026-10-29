"use client";

import { EvidenceList } from "@/components/common/evidence-list";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TechItemCard } from "@/features/tech/components/tech-item-card";
import type { Evidence } from "@/types/evidence";
import type { NewsItem } from "@/types/news";
import type { TechItem } from "@/types/tech";
import type { Topic } from "@/types/topic";
import { AgentAnalysisPanel } from "./agent-analysis-panel";
import { SourceMatrix } from "./source-matrix";
import { TopicTimeline } from "./topic-timeline";

const tabs = ["overview", "timeline", "evidence", "sources", "relatedNews", "agentAnalysis"] as const;
const tabLabels: Record<(typeof tabs)[number], string> = {
  overview: "概览",
  timeline: "时间线",
  evidence: "证据",
  sources: "来源",
  relatedNews: "相关新闻",
  agentAnalysis: "智能体分析",
};

export function TopicDetailTabs({
  topic,
  evidence,
  relatedNews,
  relatedTech,
}: {
  topic: Topic;
  evidence: Evidence[];
  relatedNews: NewsItem[];
  relatedTech: TechItem[];
}) {
  return (
    <Tabs defaultValue="overview" className="rounded-2xl border border-border bg-card p-4">
      <TabsList aria-label="主题详情" className="h-auto max-w-full flex-wrap justify-start">
        {tabs.map((tab) => (
          <TabsTrigger key={tab} value={tab}>
            {tabLabels[tab]}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="overview" className="mt-4 space-y-4">
        <p className="text-body1 leading-relaxed text-muted-foreground">{topic.executiveSummary ?? topic.summary}</p>
        {relatedTech.length ? (
          <div className="grid gap-3 md:grid-cols-2">
            {relatedTech.map((item) => (
              <TechItemCard key={item.id} item={item} compact />
            ))}
          </div>
        ) : null}
      </TabsContent>
      <TabsContent value="timeline" className="mt-4">
        <TopicTimeline items={topic.timeline ?? []} />
      </TabsContent>
      <TabsContent value="evidence" className="mt-4">
        <EvidenceList evidence={evidence} />
      </TabsContent>
      <TabsContent value="sources" className="mt-4">
        <SourceMatrix sources={topic.sourceCoverage ?? []} />
      </TabsContent>
      <TabsContent value="relatedNews" className="mt-4">
        {relatedNews.length ? (
          <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border">
            {relatedNews.map((item) => (
              <li key={item.id} className="px-4 py-3">
                <p className="text-body2 font-semibold text-foreground">{item.title}</p>
                <p className="mt-0.5 line-clamp-2 text-[13px] leading-relaxed text-muted-foreground">{item.summary}</p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="py-6 text-center text-body2 text-muted-foreground">暂无相关新闻。</p>
        )}
      </TabsContent>
      <TabsContent value="agentAnalysis" className="mt-4">
        <AgentAnalysisPanel analyses={topic.agentAnalysis ?? []} />
      </TabsContent>
    </Tabs>
  );
}
