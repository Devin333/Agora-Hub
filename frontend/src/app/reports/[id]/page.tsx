"use client";;
import { use } from "react";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
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
import { isHostedProfile } from "@/lib/deployment-profile";
import { DesktopSavedReportDetail } from "@/app/reports/desktop-report-detail";

export default function ReportDetailPage(props: { params: Promise<{ id: string }> }) {
  const params = use(props.params);
  return isHostedProfile()
    ? <DesktopSavedReportDetail reportId={params.id} />
    : <LegacyReportDetail reportId={params.id} />
}

function LegacyReportDetail({ reportId }: { reportId: string }) {
  const reportQuery = useReportDetail(reportId);
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
          <Button asChild variant="outline">
            <Link href="/reports">返回报告</Link>
          </Button>
        }
      />
    );
  }

  const markdown = data.report.markdown ?? "";

  return (
    <main className="space-y-6">
      <PageHeader
        eyebrow="生成报告"
        title={data.report.title}
        description="Markdown 报告，包含支撑主题、相关新闻、证据引用和质量元数据。"
        actions={
          <Button asChild variant="ghost">
            <Link href="/reports"><ArrowLeft size={15} strokeWidth={1.8} />返回报告</Link>
          </Button>
        }
      />

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
        <aside className="space-y-6">
          <ReportToc markdown={markdown} />
          <ReportMetadataPanel report={data.report} />
        </aside>
      </div>
    </main>
  );
}

function RelatedSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-heading3 font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  );
}

function EmptyLine({ children }: { children: React.ReactNode }) {
  return <p className="text-body2 text-muted-foreground">{children}</p>;
}
