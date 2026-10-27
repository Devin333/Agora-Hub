import { ExternalTextLink, MaturityBadge, TechTypeBadge } from "@/components/common/badges";
import { cn } from "@/lib/utils";
import type { TechItem } from "@/types/tech";

/** Rakazo card for a technology radar item; `compact` drops the evaluation lines for nested lists. */
export function TechItemCard({ item, compact = false }: { item: TechItem; compact?: boolean }) {
  return (
    <article className={cn("flex flex-col rounded-2xl border border-border p-4", compact ? "bg-background" : "bg-card")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold text-foreground">{item.name}</h3>
          {item.problem && !compact ? <p className="mt-0.5 text-label text-muted-foreground">问题：{item.problem}</p> : null}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <TechTypeBadge type={item.type} />
          <MaturityBadge maturity={item.maturity} />
        </div>
      </div>
      <p className="mt-2 line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">{item.summary}</p>
      {!compact ? (
        <div className="mt-2 grid gap-1 text-label text-muted-foreground">
          {item.agentEvaluation ? <p>智能体评估：{item.agentEvaluation}</p> : null}
          {item.referenceValue ? <p>引用价值：{item.referenceValue}</p> : null}
        </div>
      ) : null}
      {item.tags.length ? (
        <div className="mt-3 flex flex-wrap gap-1">
          {item.tags.map((tag) => (
            <span key={tag} className="rounded-md bg-muted px-2 py-0.5 text-label text-muted-foreground">
              #{tag}
            </span>
          ))}
        </div>
      ) : null}
      <div className="mt-auto flex flex-wrap items-center justify-between gap-3 pt-3">
        <div className="flex flex-wrap gap-x-2 gap-y-1">
          {item.relatedTopicNames?.map((topic) => (
            <span key={topic} className="text-label text-link">
              {topic}
            </span>
          ))}
        </div>
        <ExternalTextLink href={item.sourceUrl}>来源</ExternalTextLink>
      </div>
    </article>
  );
}
