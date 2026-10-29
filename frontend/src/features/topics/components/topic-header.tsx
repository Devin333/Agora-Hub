import { HeatScoreBadge, QualityBadge, TrendBadge } from "@/components/common/badges";
import { ScoreMeter } from "@/components/common/score-meter";
import { formatDate } from "@/lib/format";
import type { Topic } from "@/types/topic";

/** Bordered rakazo page header for a topic, with score meters and a metric strip. */
export function TopicHeader({ topic }: { topic: Topic }) {
  const chips = [...(topic.entities ?? []), ...(topic.tags ?? []).map((tag) => `#${tag}`)];
  return (
    <header className="space-y-4 border-b border-border pb-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap gap-1.5">
            <TrendBadge trend={topic.trend} />
            <HeatScoreBadge score={topic.heatScore} />
            <QualityBadge score={topic.qualityScore ?? 0} />
          </div>
          <h1 className="mt-3 text-heading2 font-semibold text-foreground">{topic.name}</h1>
          <p className="mt-1 max-w-4xl text-body1 leading-relaxed text-muted-foreground">{topic.summary}</p>
        </div>
        <div className="grid min-w-64 gap-3">
          <ScoreMeter label="热度" value={topic.heatScore} />
          <ScoreMeter label="质量" value={topic.qualityScore ?? 0} />
        </div>
      </div>
      <dl className="grid gap-2 sm:grid-cols-2 md:grid-cols-4">
        <Metric label="新闻" value={String(topic.itemCount)} />
        <Metric label="来源" value={String(topic.sourceCount)} />
        <Metric label="首次出现" value={formatDate(topic.firstSeenAt)} />
        <Metric label="更新于" value={formatDate(topic.lastSeenAt)} />
      </dl>
      {chips.length ? (
        <div className="flex flex-wrap gap-1">
          {chips.map((item) => (
            <span key={item} className="rounded-md bg-muted px-2 py-0.5 text-label text-muted-foreground">
              {item}
            </span>
          ))}
        </div>
      ) : null}
    </header>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-muted px-3 py-2">
      <dt className="text-[11px] uppercase tracking-wider text-faint-foreground">{label}</dt>
      <dd className="mt-0.5 text-body2 font-semibold tabular-nums text-foreground">{value}</dd>
    </div>
  );
}
