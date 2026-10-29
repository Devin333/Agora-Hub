import Link from "next/link";
import { Badge, QualityBadge } from "@/components/common/badges";
import { formatDate, titleCase } from "@/lib/format";
import type { Report } from "@/types/report";

export function ReportCard({ report }: { report: Report }) {
  return (
    <Link href={`/reports/${report.id}`} className="block rounded-2xl border bg-card p-4 transition-colors hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-body1 font-semibold text-foreground">{report.title}</h2>
          <p className="mt-1 text-label text-muted-foreground">由 {report.agentName ?? "Agora Hub 智能体"} 生成于 {formatDate(report.generatedAt)}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge tone="accent">{titleCase(report.reportType ?? report.type ?? "report")}</Badge>
          <Badge tone={report.status === "published" ? "good" : report.status === "failed" ? "bad" : "info"}>{titleCase(report.status)}</Badge>
        </div>
      </div>
      <div className="mt-3 grid gap-2 text-body2 text-muted-foreground sm:grid-cols-2 lg:grid-cols-4">
        <span>范围 {formatDate(report.coveredFrom)} - {formatDate(report.coveredTo)}</span>
        <span>{report.topicIds?.length ?? 0} 个主题</span>
        <span>{report.newsItemIds?.length ?? 0} 条新闻</span>
        <span>{report.evidenceIds?.length ?? 0} 条证据引用</span>
      </div>
      <div className="mt-3">
        <QualityBadge score={report.qualityScore ?? 0} />
      </div>
    </Link>
  );
}
