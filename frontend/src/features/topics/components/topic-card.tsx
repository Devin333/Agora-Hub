import Link from "next/link";
import { HeatScoreBadge, QualityBadge, TrendBadge } from "@/components/common/badges";
import { formatDate } from "@/lib/format";
import type { Topic } from "@/types/topic";

/** Rakazo card linking to a topic detail page; `dense` hides the summary for compact lists. */
export function TopicCard({ topic, dense = false }: { topic: Topic; dense?: boolean }) {
  const entities = (topic.entities ?? []).slice(0, 4);
  const tags = topic.tags ?? [];
  return (
    <Link
      href={`/topics/${topic.id}`}
      className="flex flex-col rounded-2xl border border-border bg-card p-4 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-[15px] font-semibold text-foreground">{topic.name}</h2>
          <p className="mt-0.5 text-label text-muted-foreground">{topic.category ?? "未分类"}</p>
        </div>
        <TrendBadge trend={topic.trend} />
      </div>
      {!dense ? <p className="mt-2 line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">{topic.summary}</p> : null}
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <HeatScoreBadge score={topic.heatScore} />
        <QualityBadge score={topic.qualityScore ?? 0} />
        <span className="text-label tabular-nums text-muted-foreground">
          {topic.itemCount} 条新闻 · {topic.sourceCount} 个来源
        </span>
      </div>
      <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-label tabular-nums text-faint-foreground">
        <span>首次出现 {formatDate(topic.firstSeenAt)}</span>
        <span>更新于 {formatDate(topic.lastSeenAt)}</span>
      </div>
      {entities.length || tags.length ? (
        <div className="mt-3 flex flex-wrap items-center gap-1">
          {entities.map((entity) => (
            <span key={entity} className="rounded-md bg-muted px-2 py-0.5 text-label text-muted-foreground">
              {entity}
            </span>
          ))}
          {tags.map((tag) => (
            <span key={tag} className="px-1 text-label text-link">
              #{tag}
            </span>
          ))}
        </div>
      ) : null}
    </Link>
  );
}
