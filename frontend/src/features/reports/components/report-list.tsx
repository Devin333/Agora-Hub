import { EmptyState } from "@/components/common/empty-state";
import type { Report } from "@/types/report";
import { ReportCard } from "./report-card";

export function ReportList({ reports }: { reports: Report[] }) {
  if (!reports.length) {
    return <EmptyState title="未找到报告" description="没有生成报告匹配当前筛选条件。" />;
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {reports.map((report) => (
        <ReportCard key={report.id} report={report} />
      ))}
    </div>
  );
}
