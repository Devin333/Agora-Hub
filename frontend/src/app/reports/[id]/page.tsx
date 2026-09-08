"use client";

import Link from "next/link";
import { EmptyState } from "@/components/common/empty-state";
import { ErrorState } from "@/components/common/error-state";
import { EvidenceList } from "@/components/common/evidence-list";
import { PageSkeleton } from "@/components/common/loading-skeleton";
import { MarkdownViewer } from "@/components/markdown/markdown-viewer";
import { NewsCard } from "@/features/news/components/news-card";
import { ReportMetadataPanel } from "@/features/reports/components/report-metadata-panel";
import { ReportToc } from "@/features/reports/components/report-toc";
import { useReportDetail } from "@/features/reports/hooks/use-report-detail";
import { TopicCard } from "@/features/topics/components/topic-card";

export default function ReportDetailPage({ params }: { params: { id: string } }) {
  const reportQuery = useReportDetail(params.id);
  const { data } = reportQuery;

  if (reportQuery.isLoading) {
    return <PageSkeleton />;
  }

  if (reportQuery.isError) {
    return <ErrorState title="报告加载失败" message={reportQuery.error?.message} onRetry={reportQuery.refetch} />;
  }

  if (!data.report) {
    return (
      <EmptyState
        title="未找到报告"
        description="当前真实报告目录中没有这个报告 ID。"
        action={
          <Link href="/reports" className="rounded-md border border-border px-3 py-2 text-sm text-foreground hover:bg-secondary">
            返回报告
          </Link>
        }
      />
    );
  }

  const markdown = data.report.markdown ?? "";

  return (
    <main className="space-y-6">
      <header className="rounded-lg border border-border bg-card p-5">
        <p className="text-xs font-medium uppercase text-primary">生成报告</p>
        <h1 className="mt-3 text-2xl font-semibold text-foreground">{data.report.title}</h1>
        <p className="mt-3 max-w-4xl text-sm leading-6 text-muted-foreground">
          Markdown 报告，包含支撑主题、相关新闻、证据引用和质量元数据。
        </p>
      </header>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-6">
          <MarkdownViewer markdown={markdown} />
          <RelatedSection title="相关主题">
            <div className="grid gap-3 md:grid-cols-2">
              {data.relatedTopics.length ? data.relatedTopics.map((topic) => <TopicCard key={topic.id} topic={topic} dense />) : <EmptyLine>暂无相关主题。</EmptyLine>}
            </div>
          </RelatedSection>
          <RelatedSection title="相关新闻">
            <div className="space-y-3">
              {data.relatedNews.length ? data.relatedNews.map((news) => <NewsCard key={news.id} news={news} compact />) : <EmptyLine>暂无相关新闻。</EmptyLine>}
            </div>
          </RelatedSection>
          <RelatedSection title="证据引用">
            <EvidenceList evidence={data.evidence} />
          </RelatedSection>
        </div>
        <aside className="space-y-4">
          <ReportToc markdown={markdown} />
          <ReportMetadataPanel report={data.report} />
        </aside>
      </div>
    </main>
  );
}

function RelatedSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card p-5">
      <h2 className="mb-4 text-lg font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  );
}

function EmptyLine({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}
