import { SectionPanel } from "@/components/common/section-panel";
import type { Topic } from "@/types/topic";

const chartHeight = 145;

export function TopicTrendChart({ topic }: { topic: Topic }) {
  const history = topic.trendHistory ?? [{ date: "现在", heatScore: topic.heatScore, itemCount: topic.itemCount }];
  const max = Math.max(1, ...history.map((point) => point.heatScore));
  return (
    <SectionPanel title="趋势图" description="按日期的热度变化">
      <div
        className="flex h-44 items-end gap-2"
        role="img"
        aria-label={history.map((point) => `${point.date} ${point.heatScore}`).join(", ")}
      >
        {history.map((point) => (
          <div key={point.date} className="flex flex-1 flex-col items-center gap-2">
            <div
              className="w-full rounded-t-md bg-foreground/80"
              style={{ height: `${Math.max(12, (point.heatScore / max) * chartHeight)}px` }}
              title={`${point.date}: ${point.heatScore}`}
            />
            <span className="text-caption tabular-nums text-faint-foreground">{point.date}</span>
          </div>
        ))}
      </div>
    </SectionPanel>
  );
}
