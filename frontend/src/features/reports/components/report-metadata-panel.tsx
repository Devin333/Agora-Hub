import { Badge, QualityBadge } from "@/components/common/badges";
import { formatDate, titleCase } from "@/lib/format";
import type { Report } from "@/types/report";

export function ReportMetadataPanel({ report }: { report: Report }) {
  return (
    <section className="space-y-3">
      <h2 className="text-body1 font-semibold text-foreground">元数据</h2>
      <div className="divide-y rounded-xl border text-body2">
        <Row label="类型" value={<Badge tone="accent">{titleCase(report.reportType ?? report.type ?? "report")}</Badge>} />
        <Row label="状态" value={<Badge tone={report.status === "published" ? "good" : "info"}>{titleCase(report.status)}</Badge>} />
        <Row label="生成时间" value={formatDate(report.generatedAt)} />
        <Row label="覆盖范围" value={`${formatDate(report.coveredFrom)} - ${formatDate(report.coveredTo)}`} />
        <Row label="智能体" value={report.agentName ?? "Agora Hub 智能体"} />
        <Row label="主题" value={String(report.topicIds?.length ?? 0)} />
        <Row label="新闻" value={String(report.newsItemIds?.length ?? 0)} />
        <Row label="证据" value={String(report.evidenceIds?.length ?? 0)} />
        <Row label="质量" value={<QualityBadge score={report.qualityScore ?? 0} />} />
      </div>
    </section>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium text-foreground">{value}</span>
    </div>
  );
}
