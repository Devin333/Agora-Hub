import { Card, CardContent } from "@/components/ui/card";
import { formatNumber, formatScore } from "@/lib/format";
import type { QualityResult } from "@/types/quality";

export function QualityMetrics({ results }: { results: QualityResult[] }) {
  const avgScore = results.reduce((total, result) => total + result.score, 0) / Math.max(results.length, 1);
  const reviewRequired = results.filter((result) => result.status === "review_required").length;
  const failed = results.filter((result) => result.status === "failed").length;
  const issues = results.reduce((total, result) => total + result.issueCount, 0);
  return (
    <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Metric label="结果" value={formatNumber(results.length)} detail="质量记录" />
      <Metric label="平均分" value={formatScore(avgScore)} detail="当前筛选集合" />
      <Metric label="需要复核" value={formatNumber(reviewRequired)} detail="待人工处理" tone="warning" />
      <Metric label="失败" value={formatNumber(failed)} detail={`${formatNumber(issues)} 个问题`} tone={failed ? "danger" : "success"} />
    </section>
  );
}

function Metric({ label, value, detail, tone }: { label: string; value: string; detail: string; tone?: "success" | "warning" | "danger" }) {
  const toneClass = tone === "success" ? "text-success" : tone === "warning" ? "text-warning" : tone === "danger" ? "text-destructive" : "text-foreground";
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className={`mt-2 truncate text-2xl font-semibold ${toneClass}`}>{value}</p>
        <p className="mt-1 truncate text-xs text-muted-foreground">{detail}</p>
      </CardContent>
    </Card>
  );
}
